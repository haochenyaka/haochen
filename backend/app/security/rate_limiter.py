"""基于 Redis 的速率限制中间件：按 IP/用户/路由组分级限流。

不变量：
- 未配置 Redis 时不阻断业务（限流降级跳过）；
- 限流状态以 Redis 滑动窗口计数为唯一事实，本地不持有计数器；
- 被限流的请求仍进入正常认证流水线，在中间件层以 429 拒绝（非授权拒绝）；
- 429 响应含 Retry-After 头（秒），调用方应据此退避。
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


def _rate_limit_key(request: Request, group: str) -> str:
    """限流键：按 (路由组, IP) 区分。

    优先使用 X-Forwarded-For 取真实客户端 IP，回退到 remote_addr。
    """
    ip = request.headers.get(
        "x-forwarded-for", request.client.host if request.client else "unknown"
    )
    # 取第一个 IP（X-Forwarded-For 可能是逗号分隔的链）
    client_ip = ip.split(",")[0].strip()
    return f"ratelimit:{group}:{client_ip}"


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

    async def check(self, request: Request) -> bool:
        """检查请求是否超出速率限制。返回 True 表示通过，False 表示被限流。"""
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return True

        redis = await self._get_redis()
        if redis is None:
            return True  # Redis 不可达时放行

        window, max_requests = self._get_limits(request, settings)
        key = _rate_limit_key(request, _route_group(request))
        now = time.time()
        window_start = now - window

        try:
            # 使用 pipeline 原子操作
            async with redis.pipeline(transaction=True) as pipe:
                # 移除窗口外的旧记录
                await pipe.zremrangebyscore(key, "-inf", window_start)
                # 统计窗口内请求数
                await pipe.zcard(key)
                # 添加当前请求
                await pipe.zadd(key, {f"{now}:{id(request)}": now})
                # 设置键过期
                await pipe.expire(key, window * 2)
                results = await pipe.execute()

            count = results[1]  # zcard 结果
            if count >= max_requests:
                logger.warning(
                    "速率限制 %s %s: %d/%d (窗口 %ds)",
                    _route_group(request),
                    key,
                    count,
                    max_requests,
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
