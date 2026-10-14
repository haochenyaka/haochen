"""投诉用例服务。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Complaint
from app.repositories.complaint_repository import ComplaintRepository


class ComplaintService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._complaints = ComplaintRepository(session)

    async def create(
        self,
        *,
        guest_id: UUID,
        content: str,
        order_id: UUID | None = None,
        room_no: str | None = None,
        category: str | None = None,
    ) -> Complaint:
        return await self._complaints.create(
            guest_id=guest_id,
            content=content,
            order_id=order_id,
            room_no=room_no,
            category=category,
        )
