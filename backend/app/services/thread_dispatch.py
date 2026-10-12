"""线程接力：FIFO 派发的唯一入口（仅队头 queued 请求在空闲线程上创建 AgentRun）。

调用方必须已持有线程行锁（intake / 终态收敛 / reconciler），并在提交后执行投递计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import AgentRunRepository, RequestRepository


@dataclass(frozen=True)
class DispatchPlan:
    request_id: UUID
    run_id: UUID


async def promote_head_if_idle(session: AsyncSession, thread_id: UUID) -> DispatchPlan | None:
    runs = AgentRunRepository(session)
    requests = RequestRepository(session)
    if await runs.get_active_for_thread(thread_id) is not None:
        return None
    head = await requests.get_head_queued(thread_id)
    if head is None:
        return None
    run = await runs.create_pending(
        request_id=head.id,
        thread_id=head.thread_id,
        user_id=head.user_id,
        agent_slug=head.agent_slug,
    )
    await requests.mark_dispatched(head.id)
    return DispatchPlan(request_id=head.id, run_id=run.id)
