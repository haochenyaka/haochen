"""保洁端打卡接口：上班/下班打卡、个人记录查询。

权限：staff 角色；打卡时自动执行视觉审核（人脸比对、工牌检测、工服检测）。
审核不通过（manual_review）进入复核队列，由店长手动审核（P7 管理端）。
"""

from __future__ import annotations

from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import ConflictError, DomainError, NotFoundError, ValidationFailedError
from app.security import Identity, require_roles
from app.services.attendance_service import AttendanceService
from app.vision_api import VisionClient

router = APIRouter(prefix="/staff/attendance", tags=["staff-attendance"])

staff_guard = require_roles("staff")


class ClockRequest(BaseModel):
    direction: str  # "check_in" | "check_out"
    photo_url: str


class ClockResponse(BaseModel):
    id: UUID
    direction: str
    review_status: str
    face_score: float | None
    badge_ok: bool | None
    uniform_ok: bool | None


class AttendanceRecord(BaseModel):
    id: UUID
    direction: str
    photo_url: str
    face_score: float | None
    badge_ok: bool | None
    uniform_ok: bool | None
    review_status: str
    reviewed_by: UUID | None
    reviewed_at: str | None
    created_at: str


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, ValidationFailedError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/clock", response_model=ClockResponse, status_code=201)
async def clock(
    body: ClockRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> ClockResponse:
    """上班/下班打卡。"""
    service = AttendanceService(session)
    vision = VisionClient.from_settings()
    try:
        record = await service.clock(
            identity.user_id,
            direction=body.direction,
            photo_url=body.photo_url,
            vision=vision,
        )
        await session.commit()
    except DomainError as exc:
        await session.rollback()
        _raise_http(exc)
    return ClockResponse(
        id=record.id,
        direction=record.direction,
        review_status=record.review_status,
        face_score=float(record.face_score) if record.face_score is not None else None,
        badge_ok=record.badge_ok,
        uniform_ok=record.uniform_ok,
    )


@router.get("/me", response_model=list[AttendanceRecord])
async def my_records(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
) -> list[AttendanceRecord]:
    """我自己的打卡记录（最近优先）。"""
    service = AttendanceService(session)
    try:
        records = await service.list_my_records(identity.user_id, limit=limit, offset=offset)
    except DomainError as exc:
        _raise_http(exc)
    return [
        AttendanceRecord(
            id=r.id,
            direction=r.direction,
            photo_url=r.photo_url,
            face_score=float(r.face_score) if r.face_score is not None else None,
            badge_ok=r.badge_ok,
            uniform_ok=r.uniform_ok,
            review_status=r.review_status,
            reviewed_by=r.reviewed_by,
            reviewed_at=r.reviewed_at.isoformat() if r.reviewed_at is not None else None,
            created_at=r.created_at.isoformat(),
        )
        for r in records
    ]