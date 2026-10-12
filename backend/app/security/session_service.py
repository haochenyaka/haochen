"""会话令牌：opaque 随机令牌存 Redis（滑动续期），Redis 不可用一律拒绝（fail-closed）。

令牌本身是唯一凭据，不落数据库、不写日志；解析失败与禁用账号在调用方统一按未认证处理。
"""

from __future__ import annotations

import logging
import secrets
from uuid import UUID

from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)

SESSION_KEY_PREFIX = "session:"
TOKEN_BYTES = 32


class SessionUnavailableError(RuntimeError):
    """会话服务不可用（Redis 未就绪）：签发失败关闭。"""


class SessionService:
    def __init__(self, client: Redis | None, *, ttl_seconds: int | None = None) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds or get_settings().session_ttl_seconds

    @property
    def enabled(self) -> bool:
        return self._client is not None

    @property
    def ttl_seconds(self) -> int:
        return self._ttl_seconds

    async def create(self, user_id: UUID) -> str:
        if self._client is None:
            raise SessionUnavailableError("会话存储不可用")
        token = secrets.token_urlsafe(TOKEN_BYTES)
        await self._client.set(self._key(token), str(user_id), ex=self._ttl_seconds)
        return token

    async def resolve(self, token: str) -> UUID | None:
        """校验令牌并滑动续期；令牌缺失、过期或 Redis 异常返回 None。"""
        if self._client is None or not token:
            return None
        key = self._key(token)
        try:
            value = await self._client.get(key)
            if value is None:
                return None
            await self._client.expire(key, self._ttl_seconds)
        except Exception as exc:
            logger.warning("会话解析失败，按未认证处理：%s", exc)
            return None
        try:
            return UUID(str(value))
        except ValueError:
            logger.warning("会话存储值非法，按未认证处理")
            return None

    async def revoke(self, token: str) -> None:
        if self._client is None or not token:
            return
        try:
            await self._client.delete(self._key(token))
        except Exception as exc:
            logger.warning("会话注销失败（令牌将由 TTL 到期自然失效）：%s", exc)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    @staticmethod
    def _key(token: str) -> str:
        return f"{SESSION_KEY_PREFIX}{token}"


async def create_session_service() -> SessionService:
    """进程级会话服务：Redis 不可达时返回禁用实现（登录 503、会话认证 401）。"""
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        logger.warning("会话存储不可用，登录与令牌认证将失败关闭：%s", exc)
        await client.aclose()
        return SessionService(None)
    return SessionService(client)
