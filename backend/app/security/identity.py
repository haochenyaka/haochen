"""身份解析与角色依赖：Bearer 会话令牌为主，开发态请求头为过渡（P2/P4/P5 逐端替换）。

约定：
- Bearer 令牌存在但无效（伪造/过期/已注销）时一律按未认证处理，不回退开发态请求头；
- 每次请求回读账号状态，禁用账号立即失去全部会话效力（术语表 §11）；
- 角色判定只在本模块与路由依赖处发生，repository 侧仍做最终归属校验（A6）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.repositories import UserRepository
from app.security.session_service import SessionService

logger = logging.getLogger(__name__)

DEV_USERNAME_HEADER = "X-Dev-Username"


@dataclass(frozen=True)
class Identity:
    user_id: UUID
    username: str
    role: str
    via: str  # "session" | "dev-header"


def get_session_service(request: Request) -> SessionService:
    service: SessionService | None = getattr(request.app.state, "session_service", None)
    return service if service is not None else SessionService(None)


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


async def resolve_identity(
    session: AsyncSession,
    *,
    authorization: str | None,
    dev_username: str | None,
    session_service: SessionService,
    allow_dev_header: bool,
) -> Identity | None:
    token = _bearer_token(authorization)
    if token is not None:
        user_id = await session_service.resolve(token)
        if user_id is None:
            return None
        user = await UserRepository(session).get_by_id(user_id)
        if user is None or user.status != "active":
            return None
        return Identity(user_id=user.id, username=user.username, role=user.role, via="session")

    if allow_dev_header and dev_username:
        user = await UserRepository(session).get_or_create(username=dev_username)
        if user.status != "active":
            return None
        return Identity(user_id=user.id, username=user.username, role=user.role, via="dev-header")
    return None


async def get_identity(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    x_dev_username: Annotated[str | None, Header(alias=DEV_USERNAME_HEADER)] = None,
) -> Identity | None:
    return await resolve_identity(
        session,
        authorization=request.headers.get("Authorization"),
        dev_username=x_dev_username,
        session_service=get_session_service(request),
        allow_dev_header=get_settings().dev_identity_enabled,
    )


async def require_identity(
    identity: Annotated[Identity | None, Depends(get_identity)],
) -> Identity:
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未认证或会话已失效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return identity


def require_roles(*roles: str) -> Callable[..., Awaitable[Identity]]:
    """角色依赖：不属于任一角色返回 403；身份缺失由 require_identity 统一 401。"""
    allowed = frozenset(roles)

    async def _dependency(identity: Annotated[Identity, Depends(require_identity)]) -> Identity:
        if identity.role not in allowed:
            logger.info("越权访问被拒绝：role=%s allowed=%s", identity.role, sorted(allowed))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限访问该资源")
        return identity

    return _dependency
