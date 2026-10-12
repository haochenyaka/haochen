"""C 端微信登录：code2session 换取 openid，幂等建立身份与客人档案并签发会话令牌。

约定：
- code 换取失败一律 401，不落任何用户/客人事实；渠道不可达 503（失败关闭）；
- 身份与客人档案按 openid 幂等建立，并发登录由唯一约束兜底、落败方回读既有记录；
- 会话存储不可用时 503，不回退开发态身份请求头。
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories import GuestRepository, UserRepository
from app.security import SessionUnavailableError, get_session_service
from app.wechat import WechatClient, WechatError, WechatUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest/auth", tags=["guest-auth"])

_fallback_client: WechatClient | None = None


def get_wechat_client(request: Request) -> WechatClient:
    client: WechatClient | None = getattr(request.app.state, "wechat_client", None)
    if client is not None:
        return client
    global _fallback_client
    if _fallback_client is None:
        _fallback_client = WechatClient.from_settings()
    return _fallback_client


class WechatLoginBody(BaseModel):
    code: str = Field(min_length=1, max_length=128)


class UserIdentity(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    role: str


class GuestProfile(BaseModel):
    id: UUID
    nickname: str | None
    openid: str


class WechatLoginResponse(BaseModel):
    token: str
    expires_in: int
    user: UserIdentity
    guest: GuestProfile


@router.post("/wechat", response_model=WechatLoginResponse)
async def wechat_login(
    body: WechatLoginBody,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    client: Annotated[WechatClient, Depends(get_wechat_client)],
) -> WechatLoginResponse:
    try:
        wx_session = await client.code2session(body.code)
    except WechatUnavailableError as exc:
        logger.warning("微信登录服务不可达")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信登录服务暂不可用，请稍后重试",
        ) from exc
    except WechatError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="微信登录凭证无效，请重新进入小程序"
        ) from exc

    users = UserRepository(session)
    guests = GuestRepository(session)
    user = await users.get_or_create(
        username=f"wx_{wx_session.openid}", display_name="微信客人", role="customer"
    )
    if user.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已禁用")

    guest = await guests.get_by_openid(wx_session.openid)
    if guest is None:
        guest = await guests.get_by_user_id(user.id)
    if guest is None:
        try:
            async with session.begin_nested():
                guest = await guests.create(
                    user_id=user.id, openid=wx_session.openid, nickname="微信客人"
                )
        except IntegrityError:
            # 并发登录：唯一约束裁决，落败方回读胜出记录
            guest = await guests.get_by_openid(wx_session.openid) or await guests.get_by_user_id(
                user.id
            )
            if guest is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="登录并发冲突，请重试"
                ) from None
    else:
        updates: dict[str, object] = {}
        if guest.user_id is None:
            updates["user_id"] = user.id
        if guest.openid is None:
            updates["openid"] = wx_session.openid
        if updates:
            await guests.update_fields(guest.id, updates)
            await session.refresh(guest)

    service = get_session_service(request)
    try:
        token = await service.create(user.id)
    except SessionUnavailableError as exc:
        logger.warning("微信登录失败：会话存储不可用")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="会话服务不可用"
        ) from exc
    await users.update_fields(user.id, {"last_login_at": func.now()})
    await session.commit()
    return WechatLoginResponse(
        token=token,
        expires_in=service.ttl_seconds,
        user=UserIdentity(
            id=user.id, username=user.username, display_name=user.display_name, role=user.role
        ),
        guest=GuestProfile(id=guest.id, nickname=guest.nickname, openid=guest.openid or ""),
    )
