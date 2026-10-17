"""基于 Redis 的速率限制中间件：按路由组、用户、IP 与部署租户分级限流。

不变量：
- 未配置 Redis 时不阻断业务（限流降级跳过）；
- 限流状态以 Redis 滑动窗口计数为唯一事实，本地不持有计数器；
- 被限流的请求仍进入正常认证流水线，在中间件层以 429 拒绝（非授权拒绝）；
- 429 响应含 Retry-After 头（秒），调用方应据此退避；
- 已认证请求同时计入「用户」与「IP」两个键，未认证请求只计入 IP；
- 部署级租户总额度独立于单主体配额，避免多主体合计打爆上游。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import Settings, get_settings
from app.security.session_service import SESSION_KEY_PREFIX

logger = logging.getLogger(__name__)

# 路径前缀到路由组的映射
PATH_GROUPS: list[tuple[str, str]] = [
    ("/api/guest/", "guest"),
    ("/api/staff/", "staff"),
    ("/api/admin/", "admin"),
    ("/api/webhooks/", "webhook"),
    ("/api/auth/", "auth"),
    ("/api/health", "health"),
    ("/api/device/", "device"),
    ("/chat/", "chat"),
]


def _route_group(request: Request) -> str:
    """根据请求路径匹配路由组。"""
    path = request.url.path
    for prefix, group in PATH_GROUPS:
        if path.startswith(prefix):
            return group
    return "default"


def _client_ip(request: Request, *, trust_forwarded_for: bool = True) -> str:
    """客户端 IP：仅当信任反向代理时使用 X-Forwarded-For，回退到 remote_addr。"""
    if trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # 取第一个 IP（X-Forwarded-For 可能是逗号分隔的链）
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_key(request: Request, group: str, *, trust_forwarded_for: bool = True) -> str:
    """IP 层限流键：按 (路由组, IP) 区分。"""
    return f"ratelimit:{group}:ip:{_client_ip(request, trust_forwarded_for=trust_forwarded_for)}"


def _user_key(group: str, user_id: str) -> str:
    return f"ratelimit:{group}:user:{user_id}"


def _tenant_key(group: str, tenant_id: str) -> str:
    return f"ratelimit:{group}:tenant:{tenant_id}"


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization")
    if not authorization:
        return None
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credentials.strip():
        return None
    return credentials.strip()


class RateLimitExceeded(Exception):
    """限流触发的异常，在中间件层捕获并返回 429。"""


class RateLimiter:
    """滑动窗口速率限制器。

    使用 Redis Sorted Set（ZADD/ZREMRANGEBYSCORE/ZCARD）实现滑动窗口计数。
    Redis 不可达时跳过限流。
    """

    def __init__(self) -> None:
        self._redis: Any = None

    async def _get_redis(self) -> Any:
        """懒加载 Redis 连接。"""
        if self._redis is not None:
            return self._redis
        try:
            from redis import asyncio as aioredis

            from app.config import get_settings

            settings = get_settings()
            self._redis = await aioredis.from_url(
                settings.redis_url,
                socket_connect_timeout=2,
                socket_timeout=2,
                decode_responses=True,
            )
            await self._redis.ping()
        except Exception as exc:
            logger.warning("Redis 不可达，速率限制降级跳过：%s", exc)
            self._redis = None
        return self._redis

    async def _resolve_user_id(self, request: Request, redis: Any) -> str | None:
        """从 Bearer 会话解析用户 ID；无令牌/无效令牌/Redis 异常时返回 None。"""
        token = _bearer_token(request)
        if token is None:
            return None
        try:
            value = await redis.get(f"{SESSION_KEY_PREFIX}{token}")
        except Exception as exc:
            logger.warning("限流用户解析失败，按 IP 计数：%s", exc)
            return None
        return str(value) if value else None

    def _targets(
        self,
        request: Request,
        settings: Settings,
        group: str,
        user_id: str | None,
        max_requests: int,
    ) -> list[tuple[str, int]]:
        """本次请求需要计入的 (限流键, 上限) 列表。"""
        targets: list[tuple[str, int]] = []
        if settings.rate_limit_user_enabled and user_id is not None:
            targets.append((_user_key(group, user_id), max_requests))
        targets.append(
            (
                _rate_limit_key(
                    request,
                    group,
                    trust_forwarded_for=settings.rate_limit_trust_forwarded_for,
                ),
                max_requests,
            )
        )
        if settings.rate_limit_tenant_multiplier > 0:
            targets.append(
                (
                    _tenant_key(group, settings.rate_limit_tenant_id),
                    max_requests * settings.rate_limit_tenant_multiplier,
                )
            )
        return targets

    async def check(self, request: Request) -> bool:
        """检查请求是否超出速率限制。返回 True 表示通过，False 表示被限流。"""
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return True

        redis = await self._get_redis()
        if redis is None:
            return True  # Redis 不可达时放行

        group = _route_group(request)
        window, max_requests = self._get_limits(request, settings)
        user_id = await self._resolve_user_id(request, redis)
        targets = self._targets(request, settings, group, user_id, max_requests)
        now = time.time()
        window_start = now - window
        member = f"{now}:{id(request)}"

        try:
            # 使用 pipeline 原子操作：每个键 4 条命令，结果按同样步长回读
            async with redis.pipeline(transaction=True) as pipe:
                for key, _ in targets:
                    await pipe.zremrangebyscore(key, "-inf", window_start)
                    await pipe.zcard(key)
                    await pipe.zadd(key, {member: now})
                    await pipe.expire(key, window * 2)
                results = await pipe.execute()

            for index, (key, limit) in enumerate(targets):
                count = results[index * 4 + 1]  # zcard 结果
                if count >= limit:
                    logger.warning(
                        "速率限制 %s %s: %d/%d (窗口 %ds)",
                        group,
                        key,
                        count,
                        limit,
                        window,
                    )
                    return False
            return True
        except Exception as exc:
            logger.warning("速率限制检查失败，放行：%s", exc)
            return True

    def _get_limits(self, request: Request, settings: Settings) -> tuple[int, int]:
        """按路由组返回 (窗口秒数, 最大请求数)。"""
        group = _route_group(request)
        if group == "health":
            return (5, 10)
        GUEST_LIMITS: dict[str, tuple[int, int]] = {
            "guest": (60, settings.rate_limit_guest_per_minute),
            "chat": (60, settings.rate_limit_chat_per_minute),
            "admin": (60, settings.rate_limit_admin_per_minute),
            "auth": (60, settings.rate_limit_auth_per_minute),
            "staff": (60, 60),
            "webhook": (10, 20),
        }
        return GUEST_LIMITS.get(group, (60, 60))

    async def retry_after(self, request: Request) -> int:
        """获取建议的重试等待秒数。"""
        settings = get_settings()
        window, _ = self._get_limits(request, settings)
        return window


class RateLimitMiddleware(BaseHTTPMiddleware):
    """FastAPI 速率限制中间件。

    添加到 FastAPI 应用：app.add_middleware(RateLimitMiddleware)
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._limiter = RateLimiter()

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            allowed = await self._limiter.check(request)
            if not allowed:
                retry = await self._limiter.retry_after(request)
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后重试", "retry_after": retry},
                    headers={"Retry-After": str(retry)},
                )
            return await call_next(request)
        except Exception:
            # 中间件自身失败不阻断业务
            return await call_next(request)


def setup_rate_limiter(app: FastAPI) -> None:
    """为 FastAPI 应用安装速率限制中间件（仅非健康检查路径启用）。"""
    app.add_middleware(RateLimitMiddleware)
