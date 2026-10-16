"""打卡审核用例：上班/下班打卡，AI 审核，未通过进人工复核队列。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.vocabulary import ATTENDANCE_DIRECTIONS
from app.models.domain import AttendanceCheckin
from app.repositories.attendance_repository import AttendanceCheckinRepository
from app.repositories.staff_repository import StaffRepository
from app.vision_api import VisionClient


class AttendanceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._records = AttendanceCheckinRepository(session)
        self._staff_repo = StaffRepository(session)

    async def clock(
        self,
        user_id: UUID,
        *,
        direction: str,
        photo_url: str,
        vision: VisionClient | None = None,
        face_reference: str | None = None,
    ) -> AttendanceCheckin:
        """上班/下班打卡，附带视觉审核。"""
        if direction not in ATTENDANCE_DIRECTIONS:
            raise ValidationFailedError(f"打卡方向非法：{direction}")

        staff = await self._staff_repo.get_by_user_id(user_id)
        if staff is None:
            raise NotFoundError("staff", user_id)

        # 防当日重复打卡
        existing = await self._records.get_today_record(staff.id, direction)
        if existing is not None:
            raise ConflictError(f"今日已{direction}打卡")

        vision = vision or VisionClient.from_settings()
        result = await vision.full_check(photo_path=photo_url, face_reference=face_reference)
        review_status = "auto_pass" if vision.auto_pass(result) else "manual_review"

        record = await self._records.create(
            staff_id=staff.id,
            direction=direction,
            photo_url=photo_url,
            review_status=review_status,
            face_score=round(float(result.face_score), 4),
            badge_ok=result.badge_ok,
            uniform_ok=result.uniform_ok,
        )
        return record

    async def list_my_records(
        self, user_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> list[AttendanceCheckin]:
        staff = await self._staff_repo.get_by_user_id(user_id)
        if staff is None:
            raise NotFoundError("staff", user_id)
        return await self._records.list_for_staff(staff.id, limit=limit, offset=offset)
