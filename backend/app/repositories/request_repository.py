"""请求接入事实仓储：FIFO 队头判定、排队位置与派发/取消状态迁移。

队列顺序 = seq（插入序，事务内同刻插入不并列）；状态迁移全部带守卫条件，重复调用失败关闭。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import Request
from app.repositories.support import affected_rows


class RequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID,
        agent_slug: str,
        thread_id: UUID,
        message_id: UUID,
        request_id: UUID | None = None,
    ) -> Request:
        request = Request(
            user_id=user_id,
            agent_slug=agent_slug,
            thread_id=thread_id,
            message_id=message_id,
        )
        if request_id is not None:
            request.id = request_id
        self._session.add(request)
        await self._session.flush()
        return request

    async def get_by_id(self, request_id: UUID) -> Request | None:
        return await self._session.get(Request, request_id, populate_existing=True)

    async def get_head_queued(self, thread_id: UUID) -> Request | None:
        """队头判定：线程内最早入队且仍为 queued 的请求。"""
        stmt = (
            select(Request)
            .where(Request.thread_id == thread_id, Request.status == "queued")
            .order_by(Request.seq)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def queued_position(self, request_id: UUID) -> int | None:
        """排队位置（1 起）；不存在或已非 queued 返回 None。"""
        request = await self._session.get(Request, request_id, populate_existing=True)
        if request is None or request.status != "queued":
            return None
        stmt = (
            select(func.count())
            .select_from(Request)
            .where(
                Request.thread_id == request.thread_id,
                Request.status == "queued",
                Request.seq <= request.seq,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def mark_dispatched(self, request_id: UUID) -> bool:
        result = await self._session.execute(
            update(Request)
            .where(Request.id == request_id, Request.status == "queued")
            .values(status="dispatched", dispatched_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def mark_finished(self, request_id: UUID) -> bool:
        result = await self._session.execute(
            update(Request)
            .where(Request.id == request_id, Request.status == "dispatched")
            .values(status="finished", finished_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def mark_cancelled(self, request_id: UUID) -> bool:
        """仅派发前（queued）可取消。"""
        result = await self._session.execute(
            update(Request)
            .where(Request.id == request_id, Request.status == "queued")
            .values(status="cancelled", finished_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def mark_converged(self, request_id: UUID, *, cancelled: bool) -> bool:
        """Run 到达终态后收敛请求状态（dispatched → finished / cancelled）。"""
        result = await self._session.execute(
            update(Request)
            .where(Request.id == request_id, Request.status == "dispatched")
            .values(
                status="cancelled" if cancelled else "finished",
                finished_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
