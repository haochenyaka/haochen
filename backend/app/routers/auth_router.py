"""认证路由：账号密码登录（管理端与员工端共用），Redis 会话令牌签发/注销/查询。"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories import UserRepository
from app.security import (
    Identity,
    SessionUnavailableError,
    get_session_service,
    require_identity,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS_DETAIL = "用户名或密码错误"


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class IdentityResponse(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    role: str
    via: str


class LoginResponse(BaseModel):
    token: str
    expires_in: int
    user: IdentityResponse


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginBody,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> LoginResponse:
    users = UserRepository(session)
    user = await users.get_by_username(body.username)
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS_DETAIL
        )
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已禁用")

    service = get_session_service(request)
    try:
        token = await service.create(user.id)
    except SessionUnavailableError as exc:
        logger.warning("登录失败：会话存储不可用")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="会话服务不可用"
        ) from exc
    await users.update_fields(user.id, {"last_login_at": func.now()})
    await session.commit()
    return LoginResponse(
        token=token,
        expires_in=service.ttl_seconds,
        user=IdentityResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            via="session",
        ),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    """注销当前令牌（幂等）：令牌无效或已过期同样返回 204，不暴露令牌状态。"""
    token = authorization.partition(" ")[2].strip() if authorization else ""
    await get_session_service(request).revoke(token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/me", response_model=IdentityResponse)
async def me(
    identity: Annotated[Identity, Depends(require_identity)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> IdentityResponse:
    user = await UserRepository(session).get_by_id(identity.user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不存在")
    return IdentityResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        via=identity.via,
    )
