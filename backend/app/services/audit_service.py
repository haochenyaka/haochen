"""审计埋点与读接口：Model/Tool 调用事实先落库，seq 冲突显式失败。

写入约定：
- 每条事实在其调用返回前提交：调用一旦发生即持久化，运行崩溃不丢已发生的事实；
- 同一短窗口内的并发事实合并为一次事务（窗口与批量上限可配置），降低 DB 往返；
- 耗时用单调时钟计量，受系统时间调整影响；
- 入参打码由 CredentialGuard 在调用前完成，审计只存打码结果（args_digest）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionFactory
from app.repositories import AUDIT_READ_LIMIT, AuditRepository, AuditTimeline, ThreadRepository
from app.services.agent_request_queue_service import ThreadAccessError, ThreadNotFoundError

MODEL_STATUS_COMPLETED = "completed"
MODEL_STATUS_FAILED = "failed"
TOOL_STATUS_COMPLETED = "completed"
TOOL_STATUS_FAILED = "failed"

FactKind = Literal["model", "tool", "route"]

# 同一批次可能同时涉及多张审计表：固定加锁顺序，避免跨 Run 的咨询锁形成环
_KIND_ORDER: dict[FactKind, int] = {"model": 0, "tool": 1, "route": 2}


def monotonic_ms() -> int:
    """单调时钟毫秒，用于耗时埋点。"""
    return int(time.monotonic() * 1000)


class _PendingFact:
    __slots__ = ("future", "kind", "payload")

    def __init__(
        self,
        kind: FactKind,
        payload: dict[str, Any],
        future: asyncio.Future[None] | None,
    ) -> None:
        self.kind = kind
        self.payload = payload
        self.future = future


class _AuditBatcher:
    """Run 内审计写入合并器：短窗口内的并发事实共用一次事务。

    调用方等待各自的 Future；只有所在批次提交成功后才会返回，因此"事实在返回前
    持久化"的约定不变。窗口为 0 时退化为逐条提交，与合并前的行为一致。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        window_seconds: float,
        max_facts: int,
    ) -> None:
        if window_seconds < 0:
            raise ValueError("audit batch window 不能为负")
        if max_facts < 1:
            raise ValueError("audit batch max_facts 至少为 1")
        self._session_factory = session_factory
        self._window_seconds = window_seconds
        self._max_facts = max_facts
        self._pending: list[_PendingFact] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    async def append(self, kind: FactKind, payload: dict[str, Any]) -> None:
        if self._window_seconds <= 0:
            await self._commit([_PendingFact(kind, payload, None)])
            return
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        async with self._lock:
            self._pending.append(_PendingFact(kind, payload, future))
            if self._task is None:
                self._task = asyncio.create_task(self._flush_loop())
        await future

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._window_seconds)
                async with self._lock:
                    batch, self._pending = self._pending, []
                if not batch:
                    return
                for start in range(0, len(batch), self._max_facts):
                    await self._settle(batch[start : start + self._max_facts])
        finally:
            async with self._lock:
                self._task = None
                if self._pending:
                    try:
                        self._task = asyncio.create_task(self._flush_loop())
                    except RuntimeError:
                        # 事件循环已关闭：进程级收口，未决 Future 随调用方取消
                        self._task = None

    async def _settle(self, batch: list[_PendingFact]) -> None:
        try:
            await self._commit(batch)
        except Exception as exc:
            for fact in batch:
                if fact.future is not None and not fact.future.done():
                    fact.future.set_exception(exc)
        else:
            for fact in batch:
                if fact.future is not None and not fact.future.done():
                    fact.future.set_result(None)

    async def _commit(self, batch: list[_PendingFact]) -> None:
        ordered = sorted(batch, key=lambda fact: _KIND_ORDER[fact.kind])
        async with self._session_factory() as session:
            repository = AuditRepository(session)
            for fact in ordered:
                if fact.kind == "model":
                    await repository.append_model(**fact.payload)
                elif fact.kind == "tool":
                    await repository.append_tool(**fact.payload)
                else:
                    await repository.append_route(**fact.payload)
            await session.commit()


class AuditRecorder:
    """Run 内审计记录器：middleware 与工具调用方共用同一写入口径。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        agent_slug: str,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        batch_window_ms: int | None = None,
        batch_max_facts: int | None = None,
        _batcher: _AuditBatcher | None = None,
    ) -> None:
        self._run_id = run_id
        self._agent_slug = agent_slug
        if _batcher is not None:
            self._batcher = _batcher
            return
        settings = get_settings()
        window_ms = settings.audit_batch_window_ms if batch_window_ms is None else batch_window_ms
        max_facts = settings.audit_batch_max_facts if batch_max_facts is None else batch_max_facts
        if window_ms < 0:
            raise ValueError("audit batch window 不能为负")
        self._batcher = _AuditBatcher(
            session_factory,
            window_seconds=window_ms / 1000.0,
            max_facts=max_facts,
        )

    def with_agent(self, agent_slug: str) -> AuditRecorder:
        """派生归属不同的记录器：同一 Run、同一批处理队列，只改事实的 agent_slug。

        supervisor 及其子图共用一条流水线，各子图按本器派生实例写事实，归属随装配期确定，
        不依赖运行期上下文传递。
        """
        return AuditRecorder(run_id=self._run_id, agent_slug=agent_slug, _batcher=self._batcher)

    async def model_call(
        self,
        *,
        actor_type: str,
        actor_id: str,
        model: str,
        status: str,
        error: str | None = None,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        cached_prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retrieval_refs: list[dict[str, object]] | None = None,
    ) -> None:
        await self._batcher.append(
            "model",
            {
                "run_id": self._run_id,
                "agent_slug": self._agent_slug,
                "actor_type": actor_type,
                "actor_id": actor_id,
                "model": model,
                "status": status,
                "error": error,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "cached_prompt_tokens": cached_prompt_tokens,
                "completion_tokens": completion_tokens,
                "retrieval_refs": retrieval_refs,
            },
        )

    async def tool_call(
        self,
        *,
        tool_name: str,
        status: str,
        args_digest: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> None:
        await self._batcher.append(
            "tool",
            {
                "run_id": self._run_id,
                "agent_slug": self._agent_slug,
                "tool_name": tool_name,
                "status": status,
                "args_digest": args_digest,
                "idempotency_key": idempotency_key,
                "duration_ms": duration_ms,
                "error": error,
            },
        )

    async def route_decision(
        self,
        *,
        decided_route: str,
        source: str,
        dispatched_route: str | None = None,
        reason: str | None = None,
    ) -> None:
        """记录一次路由决策事实：判定值、判定来源（llm/keyword）与最终派发目标。

        dispatched_route 与 decided_route 不同即发生过授权感知改派；为 None 表示未派发（收尾）。
        """
        await self._batcher.append(
            "route",
            {
                "run_id": self._run_id,
                "agent_slug": self._agent_slug,
                "decided_route": decided_route,
                "dispatched_route": dispatched_route,
                "source": source,
                "reason": reason,
            },
        )

    async def mark_state_reconciled(self) -> int:
        """终态对账证明：Run 收敛后标记其模型审计行已与终态对齐。"""
        async with self._batcher.session_factory() as session:
            reconciled = await AuditRepository(session).mark_run_reconciled(self._run_id)
            await session.commit()
        return reconciled


class AuditTimelineService:
    """审计时间线读用例：先做线程归属校验，再读最新 N 条 + truncated。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def thread_timeline(
        self, *, thread_id: UUID, user_id: UUID, limit: int = AUDIT_READ_LIMIT
    ) -> AuditTimeline:
        thread = await ThreadRepository(self._session).get_by_id(thread_id)
        if thread is None:
            raise ThreadNotFoundError(str(thread_id))
        if thread.user_id != user_id:
            raise ThreadAccessError(str(thread_id))
        return await AuditRepository(self._session).list_timeline(thread_id=thread_id, limit=limit)
