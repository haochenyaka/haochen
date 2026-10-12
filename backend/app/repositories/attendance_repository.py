"""打卡审核仓储。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import AttendanceCheckin


class AttendanceCheckinRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        staff_id: UUID,
        direction: str,
        photo_url: str,
        review_status: str,
        face_score: float | None = None,
        badge_ok: bool | None = None,
        uniform_ok: bool | None = None,
    ) -> AttendanceCheckin:
        record = AttendanceCheckin(
            staff_id=staff_id,
            direction=direction,
            photo_url=photo_url,
            review_status=review_status,
            face_score=face_score,
            badge_ok=badge_ok,
            uniform_ok=uniform_ok,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_id(self, record_id: UUID) -> AttendanceCheckin | None:
        return await self._session.get(
            AttendanceCheckin, record_id, populate_existing=True,
        )

    async def list_for_staff(
        self, staff_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> list[AttendanceCheckin]:
        stmt = (
            select(AttendanceCheckin)
            .where(AttendanceCheckin.staff_id == staff_id)
            .order_by(AttendanceCheckin.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_pending_review(
        self, *, limit: int = 20, offset: int = 0
    ) -> list[AttendanceCheckin]:
        stmt = (
            select(AttendanceCheckin)
            .where(AttendanceCheckin.review_status == "manual_review")
            .order_by(AttendanceCheckin.created_at)
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def update_review(
        self,
        record_id: UUID,
        *,
        review_status: str,
        reviewed_by: UUID,
    ) -> AttendanceCheckin | None:
        record = await self.get_by_id(record_id)
        if record is None:
            return None
        record.review_status = review_status
        record.reviewed_by = reviewed_by
        record.reviewed_at = datetime.now()
        await self._session.flush()
        return record

    async def get_today_record(
        self, staff_id: UUID, direction: str
    ) -> AttendanceCheckin | None:
        """获取员工当日某方向的最新打卡记录。"""
        from sqlalchemy import Date, cast

        stmt = (
            select(AttendanceCheckin)
            .where(
                AttendanceCheckin.staff_id == staff_id,
                AttendanceCheckin.direction == direction,
                cast(AttendanceCheckin.created_at, Date)
                == datetime.now().astimezone().date(),
            )
            .order_by(AttendanceCheckin.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()