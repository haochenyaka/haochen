"""门锁凭证运营接口：每日轮换人工重跑、泄露处置（吊销并轮换）与凭证元数据视图。

权限：admin/manager（店长现场处置泄露；staff 与 customer 不可达）。
明文边界（A2）：仅泄露轮换响应携带一次性明文 code，展示后即弃；日志只记房间号与版本，
元数据视图结构上不含 code_hash 与明文（对齐真实门锁的不可回读）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock.client import LockClient, LockError, LockUnavailableError
from app.lock.service import LockCredentialService
from app.lock.task_handlers import ROTATE_DAILY_TASK_TYPE, submit_rotation
from app.models.domain import LockCredential
from app.repositories.lock_credential_repository import LockCredentialRepository
from app.security import Identity, require_roles
from app.services.task_dispatch import NullTaskDispatcher, TaskDispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/lock", tags=["admin-lock"])

lock_guard = require_roles("admin", "manager")

_fallback_dispatcher = NullTaskDispatcher()


def get_task_dispatcher(request: Request) -> TaskDispatcher:
    dispatcher: TaskDispatcher | None = getattr(request.app.state, "task_dispatcher", None)
    return dispatcher if dispatcher is not None else _fallback_dispatcher


class RotationRunRequest(BaseModel):
    rotate_date: date | None = Field(default=None, description="缺省为酒店本地今天")


class RotationRunResponse(BaseModel):
    rotate_date: date
    task_type: str
    task_id: UUID
    deduplicated: bool


class LeakRotationRequest(BaseModel):
    expected_version: int | None = Field(
        default=None, description="现场界面显示的当前版本；填入则版本不符时拒绝处置"
    )


class LeakRotationResponse(BaseModel):
    room_no: str
    version: int
    code: str = Field(description="明文密码：仅本次响应一次性返回，展示后即弃")
    valid_from: datetime
    valid_to: datetime


class CredentialSummary(BaseModel):
    status: str
    reason: str
    version: int
    valid_from: datetime
    valid_to: datetime
    created_at: datetime


class RoomCredentialsResponse(BaseModel):
    room_no: str
    active: CredentialSummary | None
    next: CredentialSummary | None


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="凭证用例异常")


def _raise_lock_http(exc: LockError, *, room_no: str) -> NoReturn:
    if isinstance(exc, LockUnavailableError):
        logger.warning("门锁服务不可达：room_no=%s", room_no)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="门锁服务暂不可用，请稍后重试",
        ) from exc
    logger.warning("门锁服务拒绝请求：room_no=%s status=%s", room_no, exc.status_code)
    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail="门锁服务拒绝请求，请核对设备状态"
    ) from exc


def _summary(row: LockCredential) -> CredentialSummary:
    return CredentialSummary(
        status=row.status,
        reason=row.reason,
        version=row.version,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        created_at=row.created_at,
    )


@router.post(
    "/rotation/run",
    response_model=RotationRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_daily_rotation(
    body: RotationRunRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(lock_guard)],
    dispatcher: Annotated[TaskDispatcher, Depends(get_task_dispatcher)],
) -> RotationRunResponse:
    """人工重跑每日轮换：失败日/漏跑日的显式补跑；轮换幂等，重复触发是安全空转。"""
    rotate_date = body.rotate_date or datetime.now(HOTEL_TIMEZONE).date()
    submission = await submit_rotation(session, rotate_date=rotate_date, dispatcher=dispatcher)
    return RotationRunResponse(
        rotate_date=rotate_date,
        task_type=ROTATE_DAILY_TASK_TYPE,
        task_id=submission.task_id,
        deduplicated=submission.deduplicated,
    )


@router.get("/rooms/{room_no}/credentials", response_model=RoomCredentialsResponse)
async def get_room_credentials(
    room_no: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(lock_guard)],
) -> RoomCredentialsResponse:
    credentials = LockCredentialRepository(session)
    active = await credentials.get_active_for_room(room_no)
    pending = await credentials.get_next_for_room(room_no)
    return RoomCredentialsResponse(
        room_no=room_no,
        active=_summary(active) if active is not None else None,
        next=_summary(pending) if pending is not None else None,
    )


@router.post("/rooms/{room_no}/credential/rotate", response_model=LeakRotationResponse)
async def rotate_leaked_credential(
    room_no: str,
    body: LeakRotationRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(lock_guard)],
) -> LeakRotationResponse:
    """泄露处置：吊销当前凭证并轮换（保持原窗口），清空 next 槽位。"""
    client = LockClient.from_settings()
    try:
        fact = await LockCredentialService(session).handle_leak(
            room_no=room_no, client=client, expected_version=body.expected_version
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    except LockError as exc:
        _raise_lock_http(exc, room_no=room_no)
    finally:
        await client.aclose()
    assert fact.code is not None
    logger.info("泄露处置完成：room_no=%s version=%d", room_no, fact.version)
    return LeakRotationResponse(
        room_no=fact.room_no,
        version=fact.version,
        code=fact.code,
        valid_from=fact.valid_from,
        valid_to=fact.valid_to,
    )
