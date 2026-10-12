"""AgentRun 生命周期仓储：认领/租约/心跳/终态/过期收敛。

不变量：终态写入只接受当前租约持有者；租约过期一律收敛为
failed(worker_lease_expired)，不伪装 exactly-once；attempt 表只记事实、不可变。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import AgentRun, AgentRunAttempt
from app.repositories.support import affected_rows

ACTIVE_RUN_STATUSES: tuple[str, ...] = ("pending", "running", "cancel_requested", "interrupted")
TERMINAL_RUN_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")
LEASE_EXPIRED_ERROR = "worker_lease_expired"


class AgentRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self, *, request_id: UUID, thread_id: UUID, user_id: UUID, agent_slug: str
    ) -> AgentRun:
        run = AgentRun(
            request_id=request_id, thread_id=thread_id, user_id=user_id, agent_slug=agent_slug
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_id(self, run_id: UUID) -> AgentRun | None:
        return await self._session.get(AgentRun, run_id, populate_existing=True)

    async def get_by_request_id(self, request_id: UUID) -> AgentRun | None:
        stmt = (
            select(AgentRun)
            .where(AgentRun.request_id == request_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_active_for_thread(self, thread_id: UUID) -> AgentRun | None:
        stmt = (
            select(AgentRun)
            .where(AgentRun.thread_id == thread_id, AgentRun.status.in_(ACTIVE_RUN_STATUSES))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_pending(self, *, limit: int = 100) -> list[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.status == "pending")
            .order_by(AgentRun.created_at, AgentRun.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def claim(
        self, run_id: UUID, *, owner_token: str, lease_seconds: int
    ) -> AgentRunAttempt | None:
        """pending → running；返回本次尝试事实；未抢到（非 pending）返回 None。"""
        now = func.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        stmt = (
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == "pending")
            .values(
                status="running",
                owner_token=owner_token,
                attempt_count=AgentRun.attempt_count + 1,
                started_at=now,
                last_heartbeat_at=now,
                lease_expires_at=lease_until,
            )
            .returning(
                AgentRun.attempt_count,
                AgentRun.started_at,
                AgentRun.last_heartbeat_at,
                AgentRun.lease_expires_at,
            )
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        attempt = AgentRunAttempt(
            run_id=run_id,
            attempt_no=row.attempt_count,
            owner_token=owner_token,
            started_at=row.started_at,
            last_heartbeat_at=row.last_heartbeat_at,
            lease_expires_at=row.lease_expires_at,
        )
        self._session.add(attempt)
        await self._session.flush()
        return attempt

    async def mark_first_token(self, run_id: UUID) -> bool:
        """首个 message.delta 时落首 token 时刻；幂等（仅 first_token_at 为 NULL 时写）。

        TTFT = first_token_at - started_at（见术语表 §15）。无租约守卫：只置一次时间戳，
        即便租约已过期写入也无副作用（终态与租约由 write_terminal/reconciler 负责）。
        """
        result = await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.first_token_at.is_(None))
            .values(first_token_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def set_langfuse_trace_id(self, run_id: UUID, trace_id: str) -> bool:
        """幂等写入 langfuse_trace_id（仅 NULL 时写）。

        在模型执行前的独立短事务中调用，写入后不再改写。
        """
        result = await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.langfuse_trace_id.is_(None))
            .values(langfuse_trace_id=trace_id)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def heartbeat(self, run_id: UUID, *, owner_token: str, lease_seconds: int) -> bool:
        """续租；租约已被接管或运行已终结返回 False（调用方必须失败关闭）。"""
        now = func.now()
        lease_until = now + timedelta(seconds=lease_seconds)
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.owner_token == owner_token,
                AgentRun.status.in_(("running", "cancel_requested")),
            )
            .values(last_heartbeat_at=now, lease_expires_at=lease_until)
            .execution_options(synchronize_session=False)
        )
        await self._session.execute(
            update(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run_id,
                AgentRunAttempt.owner_token == owner_token,
                AgentRunAttempt.finished_at.is_(None),
            )
            .values(last_heartbeat_at=now, lease_expires_at=lease_until)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def write_terminal(
        self,
        run_id: UUID,
        *,
        owner_token: str,
        status: str,
        error: str | None = None,
        error_class: str | None = None,
        token_usage: dict[str, object] | None = None,
        langfuse_trace_id: str | None = None,
    ) -> bool:
        """当前租约持有者写终态并清除租约；同一事务内收敛未终结 attempt 事实。"""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"非终态状态不可作为终态写入：{status}")
        now = func.now()
        values: dict[str, object] = {
            "status": status,
            "error": error,
            "owner_token": None,
            "lease_expires_at": None,
            "finished_at": now,
        }
        if token_usage is not None:
            values["token_usage"] = token_usage
        if langfuse_trace_id is not None:
            values["langfuse_trace_id"] = langfuse_trace_id
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.owner_token == owner_token,
                AgentRun.status.in_(("running", "cancel_requested")),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if affected_rows(result) != 1:
            return False
        await self._session.execute(
            update(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run_id,
                AgentRunAttempt.owner_token == owner_token,
                AgentRunAttempt.finished_at.is_(None),
            )
            .values(finished_at=now, outcome=status, error_class=error_class, error=error)
            .execution_options(synchronize_session=False)
        )
        return True

    async def mark_interrupted(self, run_id: UUID, *, owner_token: str) -> bool:
        """running → interrupted（等待回答/审批，不占租约，可被 resume 恢复）。"""
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id == run_id,
                AgentRun.owner_token == owner_token,
                AgentRun.status == "running",
            )
            .values(status="interrupted", owner_token=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )
        if affected_rows(result) != 1:
            return False
        await self._session.execute(
            update(AgentRunAttempt)
            .where(
                AgentRunAttempt.run_id == run_id,
                AgentRunAttempt.owner_token == owner_token,
                AgentRunAttempt.finished_at.is_(None),
            )
            .values(finished_at=func.now(), outcome="interrupted")
            .execution_options(synchronize_session=False)
        )
        return True

    async def cancel_pending(self, run_id: UUID) -> bool:
        """派发前取消：pending 直接终结（尚无 worker 持有租约）。"""
        result = await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == "pending")
            .values(status="cancelled", finished_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def request_cancel(self, run_id: UUID) -> bool:
        """运行中取消意图先落库；worker 复核数据库后写终态（Redis 仅加速感知）。"""
        result = await self._session.execute(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == "running")
            .values(status="cancel_requested")
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def list_expired_lease_candidates(self, *, limit: int = 50) -> list[tuple[UUID, UUID]]:
        """只读探测过期租约候选 (run_id, thread_id)；是否收敛留待持有线程行锁后复核。"""
        stmt = (
            select(AgentRun.id, AgentRun.thread_id)
            .where(
                AgentRun.status.in_(("running", "cancel_requested")),
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at < func.now(),
            )
            .order_by(AgentRun.lease_expires_at)
            .limit(limit)
        )
        return [(row.id, row.thread_id) for row in (await self._session.execute(stmt)).all()]

    async def converge_expired(self, run_ids: Sequence[UUID]) -> list[UUID]:
        """带守卫地收敛过期租约为 failed(worker_lease_expired)；返回真正收敛的 id。

        不使用行锁：调用方须已持有相关线程行锁，保证与 intake 相同的 线程→Run 加锁顺序。
        """
        if not run_ids:
            return []
        now = func.now()
        result = await self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.id.in_(run_ids),
                AgentRun.status.in_(("running", "cancel_requested")),
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at < now,
            )
            .values(
                status="failed",
                error=LEASE_EXPIRED_ERROR,
                owner_token=None,
                lease_expires_at=None,
                finished_at=now,
            )
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        converged = [row.id for row in result.all()]
        if not converged:
            return []
        await self._session.execute(
            update(AgentRunAttempt)
            .where(AgentRunAttempt.run_id.in_(converged), AgentRunAttempt.finished_at.is_(None))
            .values(
                finished_at=now,
                outcome="lease_expired",
                error_class=LEASE_EXPIRED_ERROR,
                error=LEASE_EXPIRED_ERROR,
            )
            .execution_options(synchronize_session=False)
        )
        return converged
