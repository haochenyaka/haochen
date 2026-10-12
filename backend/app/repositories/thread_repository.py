"""线程仓储：会话行锁是 intake/resume/continue/接力的统一互斥点。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import Thread


class ThreadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, user_id: UUID, agent_slug: str, title: str | None = None) -> Thread:
        thread = Thread(user_id=user_id, agent_slug=agent_slug, title=title)
        self._session.add(thread)
        await self._session.flush()
        return thread

    async def get_by_id(self, thread_id: UUID) -> Thread | None:
        return await self._session.get(Thread, thread_id, populate_existing=True)

    async def get_for_update(self, thread_id: UUID) -> Thread | None:
        stmt = (
            select(Thread)
            .where(Thread.id == thread_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def touch(self, thread_id: UUID) -> None:
        await self._session.execute(
            update(Thread)
            .where(Thread.id == thread_id)
            .values(updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
