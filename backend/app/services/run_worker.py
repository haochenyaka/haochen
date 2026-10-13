"""Run 执行器：认领 → 心跳 → 执行体 → 终态收敛 → 接力派发。

不变量：
- 心跳走独立会话（独立连接），与执行体写库互不阻塞；心跳被数据库证实失效
  （运行已终结或租约被接管）即置失败标志，收尾时不写终态，失败关闭；
- 终态写入只接受当前租约持有者（write_terminal 守卫）；过期租约由 reconciler
  收敛为 failed(worker_lease_expired)，不伪装 exactly-once；
- 终态写入、请求收敛与队头接力在同一线程行锁内完成；ARQ 投递一律在提交之后；
- 运行事件在对应事实提交之后发射（投影可缺失，事实不可回滚）：run.started 在认领后、
  终态事件在终态提交后；事件发布失败不影响运行；
- 取消尽力而为：意图先落库，执行体以数据库为准复核并及时中止；若执行体未及时
  中止而正常完成，按 completed 收敛；若因取消而失败，按 cancelled 收敛；
- 加锁顺序恒为 线程 → Run：reconciler 先锁线程再做带守卫的批量收敛。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.middlewares.base import SteerSuperseded
from app.db import SessionFactory
from app.repositories import (
    AgentRunRepository,
    AuditRepository,
    RequestRepository,
    ThreadRepository,
)
from app.services.run_body import AgentRunBody
from app.services.run_dispatch import NullRunDispatcher, RunDispatcher, dispatch_run
from app.services.run_event_bus import RunEventBus
from app.services.run_events import RunEventEmitter
from app.services.run_types import RunIdentity, RunInterrupted, RunResult
from app.services.thread_dispatch import DispatchPlan, promote_head_if_idle

logger = logging.getLogger(__name__)

LEASE_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 20
RECOVERY_BATCH_SIZE = 200

RunOutcome = Literal[
    "completed", "failed", "cancelled", "interrupted", "not_claimable", "lease_lost"
]


class RunCancelled(Exception):
    """执行体复核数据库后确认取消意图。"""


@dataclass(frozen=True)
class RunClaim:
    identity: RunIdentity
    owner_token: str
    attempt_no: int


class RunContext:
    """执行体上下文：身份、租约归属、取消复核与事件发射；执行体自行开会话写消息与审计。"""

    def __init__(
        self,
        *,
        identity: RunIdentity,
        owner_token: str,
        attempt_no: int,
        session_factory: async_sessionmaker[AsyncSession],
        cancel_event: asyncio.Event,
        events: RunEventEmitter,
    ) -> None:
        self.identity = identity
        self.owner_token = owner_token
        self.attempt_no = attempt_no
        self.session_factory = session_factory
        self.events = events
        self._cancel_event = cancel_event

    @property
    def run_id(self) -> UUID:
        return self.identity.run_id

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def cancel_event(self) -> asyncio.Event:
        """Run 级取消信号：装配期注入给 Steer 等工位（置位即请求中止本 Run）。"""
        return self._cancel_event

    async def ensure_not_cancelled(self) -> None:
        """以数据库为准复核取消意图；已请求取消则抛出 RunCancelled。"""
        async with self.session_factory() as session:
            run = await AgentRunRepository(session).get_by_id(self.identity.run_id)
        if run is None or run.status == "cancel_requested":
            self._cancel_event.set()
            raise RunCancelled(f"取消请求已生效：run_id={self.identity.run_id}")


class RunBody(Protocol):
    async def __call__(self, ctx: RunContext) -> RunResult | RunInterrupted | None: ...


class RunExecutor:
    def __init__(
        self,
        body: RunBody,
        *,
        dispatcher: RunDispatcher | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        lease_seconds: int = LEASE_SECONDS,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        event_bus: RunEventBus | None = None,
    ) -> None:
        self._body = body
        self._dispatcher = dispatcher
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._event_bus = event_bus

    async def execute(self, run_id: UUID) -> RunOutcome:
        claim = await self._claim(run_id)
        if claim is None:
            logger.info("run not claimable (already terminal or gone): run_id=%s", run_id)
            return "not_claimable"

        identity = claim.identity
        events = RunEventEmitter(
            run_id=identity.run_id,
            thread_id=identity.thread_id,
            agent_slug=identity.agent_slug,
            bus=self._event_bus,
            on_first_token=lambda: self._record_first_token(identity.run_id),
        )
        await events.run_started()

        cancel_event = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(claim, cancel_event=cancel_event, lease_lost=lease_lost)
        )
        try:
            if await self._cancel_requested(run_id):
                return await self._finalize(claim, status="cancelled", events=events)

            ctx = RunContext(
                identity=claim.identity,
                owner_token=claim.owner_token,
                attempt_no=claim.attempt_no,
                session_factory=self._session_factory,
                cancel_event=cancel_event,
                events=events,
            )
            try:
                result = await self._body(ctx)
            except RunCancelled:
                return await self._finalize(claim, status="cancelled", events=events)
            except SteerSuperseded:
                # 更新的用户消息已接管：按取消收敛，新请求由接力链路派发
                return await self._finalize(claim, status="cancelled", events=events)
            except Exception as exc:
                error_class = type(exc).__name__
                error = str(exc) or error_class
                logger.exception("run body failed: run_id=%s", run_id)
                if cancel_event.is_set() or await self._cancel_requested(run_id):
                    return await self._finalize(claim, status="cancelled", events=events)
                return await self._finalize(
                    claim, status="failed", error=error, error_class=error_class, events=events
                )

            if lease_lost.is_set():
                logger.warning("run finished after lease loss; terminal write skipped: %s", run_id)
                return "lease_lost"
            if isinstance(result, RunInterrupted):
                # 危险工具审批挂起：不写终态、不接力，等人工决定后 resume
                return await self._mark_interrupted(claim, result, events)
            return await self._finalize(claim, status="completed", result=result, events=events)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _claim(self, run_id: UUID) -> RunClaim | None:
        owner_token = uuid4().hex
        async with self._session_factory() as session:
            runs = AgentRunRepository(session)
            attempt = await runs.claim(
                run_id, owner_token=owner_token, lease_seconds=self._lease_seconds
            )
            if attempt is None:
                # 首次认领失败：可能是审批挂起的 interrupted Run 在 resume
                attempt = await runs.claim_resume(
                    run_id, owner_token=owner_token, lease_seconds=self._lease_seconds
                )
            if attempt is None:
                await session.rollback()
                return None
            run = await runs.get_by_id(run_id)
            if run is None:
                await session.rollback()
                return None
            claim = RunClaim(
                identity=RunIdentity(
                    run_id=run.id,
                    request_id=run.request_id,
                    thread_id=run.thread_id,
                    user_id=run.user_id,
                    agent_slug=run.agent_slug,
                ),
                owner_token=owner_token,
                attempt_no=attempt.attempt_no,
            )
            await session.commit()
            return claim

    async def _heartbeat_loop(
        self,
        claim: RunClaim,
        *,
        cancel_event: asyncio.Event,
        lease_lost: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                async with self._session_factory() as session:
                    runs = AgentRunRepository(session)
                    alive = await runs.heartbeat(
                        claim.identity.run_id,
                        owner_token=claim.owner_token,
                        lease_seconds=self._lease_seconds,
                    )
                    run = await runs.get_by_id(claim.identity.run_id)
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库瞬时不可用：继续续租尝试；若租约真的过期，reconciler 会收敛，
                # 收尾的 write_terminal 守卫会拒绝我们写终态，失败关闭由数据兜底。
                logger.warning(
                    "run heartbeat error: run_id=%s", claim.identity.run_id, exc_info=True
                )
                continue

            status = run.status if run is not None else None
            if alive:
                if status == "cancel_requested":
                    cancel_event.set()
                continue

            logger.warning("run lease lost: run_id=%s status=%s", claim.identity.run_id, status)
            lease_lost.set()
            return

    async def _cancel_requested(self, run_id: UUID) -> bool:
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get_by_id(run_id)
        return run is not None and run.status == "cancel_requested"

    async def _record_first_token(self, run_id: UUID) -> None:
        """首 token 埋点：独立会话落 agent_runs.first_token_at（幂等，仅 NULL 时写）。"""
        async with self._session_factory() as session:
            await AgentRunRepository(session).mark_first_token(run_id)
            await session.commit()

    async def _finalize(
        self,
        claim: RunClaim,
        *,
        status: str,
        events: RunEventEmitter,
        error: str | None = None,
        error_class: str | None = None,
        result: RunResult | None = None,
    ) -> RunOutcome:
        identity = claim.identity
        async with self._session_factory() as session:
            await ThreadRepository(session).get_for_update(identity.thread_id)
            written = await AgentRunRepository(session).write_terminal(
                identity.run_id,
                owner_token=claim.owner_token,
                status=status,
                error=error,
                error_class=error_class,
                token_usage=result.token_usage if result is not None else None,
                langfuse_trace_id=result.langfuse_trace_id if result is not None else None,
            )
            if not written:
                await session.rollback()
                logger.warning(
                    "terminal write rejected (lease lost or already terminal): run_id=%s status=%s",
                    identity.run_id,
                    status,
                )
                return "lease_lost"
            await RequestRepository(session).mark_converged(
                identity.request_id, cancelled=status == "cancelled"
            )
            await AuditRepository(session).mark_run_reconciled(identity.run_id)
            plan = await promote_head_if_idle(session, identity.thread_id)
            await session.commit()

        # 终态事件在提交之后发射；终态写入被拒时不发射（由 reconciler 收敛后再投影）
        await events.run_finished(
            status=status,
            error=error,
            error_class=error_class,
            token_usage=result.token_usage if result is not None else None,
        )
        await dispatch_run(self._dispatcher, plan.run_id if plan is not None else None)
        return cast(RunOutcome, status)

    async def _mark_interrupted(
        self, claim: RunClaim, result: RunInterrupted, events: RunEventEmitter
    ) -> RunOutcome:
        """危险工具审批挂起：Run 转 interrupted（不占租约、非终态），发 interrupt 事件。

        不接力队头（线程仍被本 Run 占用，等审批恢复）；请求不收敛。人工决定后由恢复入口
        重新投递同一 Run（`claim_resume` 认领），图从检查点续跑。
        """
        identity = claim.identity
        async with self._session_factory() as session:
            written = await AgentRunRepository(session).mark_interrupted(
                identity.run_id, owner_token=claim.owner_token
            )
            await session.commit()
        if not written:
            logger.warning("mark_interrupted rejected: run_id=%s", identity.run_id)
            return "lease_lost"
        for item in result.approvals:
            await events.interrupt(
                tool=str(item.get("tool_name", "")),
                call_id=str(item.get("call_id", "")),
                reason=str(item.get("reason", "")),
            )
        # 终态收敛事件：SSE 据此收流（避免订阅方在非终态的 interrupted Run 上挂起）
        await events.run_finished(status="interrupted")
        return "interrupted"


async def repost_pending_runs(
    dispatcher: RunDispatcher | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    batch_size: int = RECOVERY_BATCH_SIZE,
) -> int:
    """补发 pending Run（提交后的投递意图，不依赖 Redis 到达保证）。"""
    async with session_factory() as session:
        run_ids = [
            run.id for run in await AgentRunRepository(session).list_pending(limit=batch_size)
        ]
    for run_id in run_ids:
        await dispatch_run(dispatcher, run_id)
    if run_ids:
        logger.info("reposted pending runs: count=%d", len(run_ids))
    return len(run_ids)


async def reconcile_expired_runs(
    dispatcher: RunDispatcher | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    batch_size: int = RECOVERY_BATCH_SIZE,
) -> int:
    """收敛过期租约并接力：先按确定性顺序锁线程行，再带守卫批量收敛（线程 → Run）。"""
    async with session_factory() as session:
        runs = AgentRunRepository(session)
        candidates = await runs.list_expired_lease_candidates(limit=batch_size)
        if not candidates:
            return 0

        threads = ThreadRepository(session)
        for thread_id in sorted({thread_id for _, thread_id in candidates}, key=str):
            await threads.get_for_update(thread_id)

        converged = await runs.converge_expired([run_id for run_id, _ in candidates])
        if not converged:
            await session.rollback()
            return 0

        requests = RequestRepository(session)
        plans: list[DispatchPlan] = []
        for run_id in converged:
            run = await runs.get_by_id(run_id)
            if run is None:
                continue
            await requests.mark_converged(run.request_id, cancelled=False)
            plan = await promote_head_if_idle(session, run.thread_id)
            if plan is not None:
                plans.append(plan)
        await session.commit()

    for plan in plans:
        await dispatch_run(dispatcher, plan.run_id)
    logger.warning("reconciled expired run leases: count=%d", len(converged))
    return len(converged)


def context_dispatcher(ctx: dict[str, Any]) -> RunDispatcher:
    dispatcher = ctx.get("run_dispatcher")
    if dispatcher is None:
        dispatcher = NullRunDispatcher()
        ctx["run_dispatcher"] = dispatcher
    return cast(RunDispatcher, dispatcher)


def context_session_factory(ctx: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    factory = ctx.get("session_factory")
    if factory is None:
        return SessionFactory
    return cast("async_sessionmaker[AsyncSession]", factory)


def context_event_bus(ctx: dict[str, Any]) -> RunEventBus | None:
    return cast(RunEventBus | None, ctx.get("run_event_bus"))


async def execute_agent_run(ctx: dict[str, Any], run_id: str) -> str:
    """ARQ 作业入口：执行一次 Run；job 名与 run_dispatch.RUN_JOB_NAME 一致。

    可用 `ctx["checkpointer"]` 注入检查点实现（测试用内存实现），默认取进程级
    PostgreSQL 检查点；`ctx["run_event_bus"]` 缺省时事件不发射（投影可缺失）。
    """
    executor = RunExecutor(
        AgentRunBody(checkpointer=ctx.get("checkpointer")),
        dispatcher=context_dispatcher(ctx),
        session_factory=context_session_factory(ctx),
        event_bus=context_event_bus(ctx),
    )
    return await executor.execute(UUID(run_id))


async def repost_pending_job(ctx: dict[str, Any]) -> int:
    return await repost_pending_runs(
        context_dispatcher(ctx), session_factory=context_session_factory(ctx)
    )


async def reconcile_expired_job(ctx: dict[str, Any]) -> int:
    return await reconcile_expired_runs(
        context_dispatcher(ctx), session_factory=context_session_factory(ctx)
    )
