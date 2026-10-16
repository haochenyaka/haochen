"""管理端记忆管理服务：记忆列表/详情/敏感标记/软删 + MemoryProvider 实现。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.middlewares.memory import MemoryEntry as AgentMemoryEntry
from app.agents.middlewares.memory import MemoryProvider
from app.models.domain import Guest, GuestMemory


@dataclass
class MemoryView:
    id: str
    guest_id: str
    guest_nickname: str | None
    guest_user_id: str | None
    category: str
    content: str
    sensitive: bool
    source: str | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class AdminMemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_memories(
        self,
        *,
        guest_search: str | None = None,
        category: str | None = None,
        sensitive: bool | None = None,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[MemoryView], int]:
        base = select(GuestMemory, Guest.nickname, Guest.user_id).join(
            Guest, GuestMemory.guest_id == Guest.id
        )
        if not include_deleted:
            base = base.where(GuestMemory.deleted_at.is_(None))
        if category:
            base = base.where(GuestMemory.category == category)
        if sensitive is not None:
            base = base.where(GuestMemory.sensitive.is_(sensitive))
        if guest_search:
            base = base.where(Guest.nickname.ilike(f"%{guest_search}%"))

        # Count
        count_q = select(sa_func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_q)).scalar() or 0

        query = base.order_by(GuestMemory.created_at.desc()).offset(offset).limit(limit)
        rows = (await self._session.execute(query)).all()

        items = [
            MemoryView(
                id=str(row.GuestMemory.id),
                guest_id=str(row.GuestMemory.guest_id),
                guest_nickname=row.nickname,
                guest_user_id=str(row.user_id) if row.user_id else None,
                category=row.GuestMemory.category,
                content=row.GuestMemory.content,
                sensitive=row.GuestMemory.sensitive,
                source=row.GuestMemory.source,
                created_at=row.GuestMemory.created_at,
                updated_at=row.GuestMemory.updated_at,
                deleted_at=row.GuestMemory.deleted_at,
            )
            for row in rows
        ]
        return items, total

    async def get_memory(self, memory_id: UUID) -> MemoryView | None:
        stmt = (
            select(GuestMemory, Guest.nickname, Guest.user_id)
            .join(Guest, GuestMemory.guest_id == Guest.id)
            .where(GuestMemory.id == memory_id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return MemoryView(
            id=str(row.GuestMemory.id),
            guest_id=str(row.GuestMemory.guest_id),
            guest_nickname=row.nickname,
            guest_user_id=str(row.user_id) if row.user_id else None,
            category=row.GuestMemory.category,
            content=row.GuestMemory.content,
            sensitive=row.GuestMemory.sensitive,
            source=row.GuestMemory.source,
            created_at=row.GuestMemory.created_at,
            updated_at=row.GuestMemory.updated_at,
            deleted_at=row.GuestMemory.deleted_at,
        )

    async def update_memory(
        self,
        memory_id: UUID,
        *,
        sensitive: bool | None = None,
        category: str | None = None,
    ) -> MemoryView | None:
        stmt = select(GuestMemory).where(GuestMemory.id == memory_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        if sensitive is not None:
            row.sensitive = sensitive
        if category is not None:
            row.category = category
        await self._session.commit()
        return await self.get_memory(memory_id)

    async def soft_delete(self, memory_id: UUID) -> bool:
        stmt = select(GuestMemory).where(GuestMemory.id == memory_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        row.deleted_at = datetime.now()
        await self._session.commit()
        return True

    async def restore(self, memory_id: UUID) -> bool:
        stmt = select(GuestMemory).where(GuestMemory.id == memory_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        row.deleted_at = None
        await self._session.commit()
        return True

    async def list_categories(self) -> list[str]:
        stmt = (
            select(GuestMemory.category)
            .where(GuestMemory.deleted_at.is_(None))
            .distinct()
            .order_by(GuestMemory.category)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def delete_permanently(self, memory_id: UUID) -> bool:
        """物理删除（仅 admin）。"""
        stmt = select(GuestMemory).where(GuestMemory.id == memory_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True


class DbMemoryProvider(MemoryProvider):
    """MemoryProvider 的 DB 实现：从 guest_memories 表按 user_id 读取记忆。

    持有 sessionmaker 而非会话实例——工位在装配期构建且跨整个 Run，而 Run 期间每次
    模型调用都要读记忆；短开会话避免长生命周期会话跨 await 与并发，口径与工具侧一致。
    通过 User → Guest → GuestMemory 链路查找当前用户关联客人 + 所有客人的记忆。
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load(self, *, user_id: UUID, thread_id: UUID) -> Sequence[AgentMemoryEntry]:
        async with self._session_factory() as session:
            stmt = (
                select(GuestMemory)
                .join(Guest, GuestMemory.guest_id == Guest.id)
                .where(
                    Guest.user_id == user_id,
                    GuestMemory.deleted_at.is_(None),
                    GuestMemory.sensitive.is_(False),
                )
                .order_by(GuestMemory.created_at.desc())
            )
            rows = (await session.execute(stmt)).scalars().all()
        return [AgentMemoryEntry(text=row.content, source=row.source or "") for row in rows]
