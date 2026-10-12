"""审计埋点与读接口：Model/Tool 调用事实先落库，seq 冲突显式失败。

写入约定：
- 每条事实用独立事务提交（提交后才返回）：调用一旦发生即持久化，运行崩溃不丢已发生的事实；
- 耗时用单调时钟计量，受系统时间调整影响；
- 入参打码由 CredentialGuard 在调用前完成，审计只存打码结果（args_digest）。
"""

from __future__ import annotations

import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.repositories import AUDIT_READ_LIMIT, AuditRepository, AuditTimeline, ThreadRepository
from app.services.agent_request_queue_service import ThreadAccessError, ThreadNotFoundError

MODEL_STATUS_COMPLETED = "completed"
MODEL_STATUS_FAILED = "failed"
TOOL_STATUS_COMPLETED = "completed"
TOOL_STATUS_FAILED = "failed"


def monotonic_ms() -> int:
    """单调时钟毫秒，用于耗时埋点。"""
    return int(time.monotonic() * 1000)


class AuditRecorder:
    """Run 内审计记录器：middleware 与工具调用方共用同一写入口径。"""

    def __init__(
        self,
        *,
        run_id: UUID,
        agent_slug: str,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    ) -> None:
        self._run_id = run_id
        self._agent_slug = agent_slug
        self._session_factory = session_factory

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
        async with self._session_factory() as session:
            await AuditRepository(session).append_model(
                run_id=self._run_id,
                agent_slug=self._agent_slug,
                actor_type=actor_type,
                actor_id=actor_id,
                model=model,
                status=status,
                error=error,
                duration_ms=duration_ms,
                prompt_tokens=prompt_tokens,
                cached_prompt_tokens=cached_prompt_tokens,
                completion_tokens=completion_tokens,
                retrieval_refs=retrieval_refs,
            )
            await session.commit()

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
        async with self._session_factory() as session:
            await AuditRepository(session).append_tool(
                run_id=self._run_id,
                agent_slug=self._agent_slug,
                tool_name=tool_name,
                status=status,
                args_digest=args_digest,
                idempotency_key=idempotency_key,
                duration_ms=duration_ms,
                error=error,
            )
            await session.commit()

    async def mark_state_reconciled(self) -> int:
        """终态对账证明：Run 收敛后标记其模型审计行已与终态对齐。"""
        async with self._session_factory() as session:
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
