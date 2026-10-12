"""客人房间凭证元数据接口：仅限本人未终态订单所属房间，只回元数据（A6 + A2 取证入口）。

响应不含 code_hash 与设备凭证号：门锁密码由门锁服务在执行时下发，服务端不落明文（A2）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import DomainError, NotFoundError, PermissionDeniedError
from app.security import Identity, require_identity
from app.services.room_access_service import RoomAccessService

router = APIRouter(prefix="/guest/rooms", tags=["guest-room"])


class CredentialMetadataResponse(BaseModel):
    room_no: str
    status: str
    reason: str
    version: int
    valid_from: datetime
    valid_to: datetime


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="凭证用例异常")


@router.get("/{room_no}/credential", response_model=CredentialMetadataResponse)
async def get_room_credential_metadata(
    room_no: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> CredentialMetadataResponse:
    service = RoomAccessService(session)
    try:
        credential = await service.credential_metadata(identity.user_id, room_no)
    except DomainError as exc:
        _raise_http(exc)
    return CredentialMetadataResponse(
        room_no=credential.room_no,
        status=credential.status,
        reason=credential.reason,
        version=credential.version,
        valid_from=credential.valid_from,
        valid_to=credential.valid_to,
    )
