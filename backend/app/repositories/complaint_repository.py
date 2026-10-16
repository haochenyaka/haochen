"""投诉仓储。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Complaint


class ComplaintRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        guest_id: UUID,
        content: str,
        order_id: UUID | None = None,
        room_no: str | None = None,
        category: str | None = None,
    ) -> Complaint:
        complaint = Complaint(
            guest_id=guest_id,
            order_id=order_id,
            room_no=room_no,
            category=category,
            content=content,
        )
        self._session.add(complaint)
        await self._session.flush()
        return complaint

    async def list_for_guest(self, guest_id: UUID, limit: int = 20) -> list[Complaint]:
        stmt = (
            select(Complaint)
            .where(Complaint.guest_id == guest_id)
            .order_by(Complaint.created_at)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())
