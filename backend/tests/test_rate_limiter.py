"""速率限制中间件单元测试（不依赖 Redis / 数据库）。"""

from __future__ import annotations

from unittest.mock import patch

from fastapi import Request
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.security.rate_limiter import (
    RateLimiter,
    _rate_limit_key,
    _route_group,
)


def test_route_group_guest() -> None:
    """/api/guest/ 路径映射到 guest 组。"""
    request = _make_request("/api/guest/rooms")
    assert _route_group(request) == "guest"


def test_route_group_admin() -> None:
    """/api/admin/ 路径映射到 admin 组。"""
    request = _make_request("/api/admin/reports/overview")
    assert _route_group(request) == "admin"


def test_route_group_auth() -> None:
    """/api/auth/ 路径映射到 auth 组。"""
    request = _make_request("/api/auth/login")
    assert _route_group(request) == "auth"


def test_route_group_health() -> None:
    """/api/health 路径映射到 health 组。"""
    request = _make_request("/api/health")
    assert _route_group(request) == "health"


def test_route_group_staff() -> None:
    """/api/staff/ 路径映射到 staff 组。"""
    request = _make_request("/api/staff/attendance")
    assert _route_group(request) == "staff"


def test_route_group_webhook() -> None:
    """/api/webhooks/ 路径映射到 webhook 组。"""
    request = _make_request("/api/webhooks/wechat")
    assert _route_group(request) == "webhook"


def test_route_group_chat() -> None:
    """/chat/ 路径映射到 chat 组。"""
    request = _make_request("/chat/stream")
    assert _route_group(request) == "chat"


def test_route_group_device() -> None:
    """/api/device/ 路径映射到 device 组。"""
    request = _make_request("/api/device/lock")
    assert _route_group(request) == "device"


def test_route_group_default() -> None:
    """未匹配的路径映射到 default 组。"""
    request = _make_request("/unknown/path")
    assert _route_group(request) == "default"


def test_rate_limit_key_format() -> None:
    """限流键格式为 ratelimit:{group}:{ip}。"""
    request = _make_request("/api/guest/rooms", client_host="192.168.1.1")
    key = _rate_limit_key(request, "guest")
    assert key == "ratelimit:guest:192.168.1.1"


def test_rate_limit_key_x_forwarded_for() -> None:
    """X-Forwarded-For 优先于 client host。"""
    request = _make_request(
        "/api/admin/overview",
        client_host="10.0.0.1",
        headers={"x-forwarded-for": "203.0.113.5, 10.0.0.1"},
    )
    key = _rate_limit_key(request, "admin")
    assert key == "ratelimit:admin:203.0.113.5"


def test_limit_settings_guest() -> None:
    """guest 组限流配置：60 秒窗口，rate_limit_guest_per_minute。"""
    settings = Settings(rate_limit_guest_per_minute=30)
    limiter = RateLimiter()
    request = _make_request("/api/guest/")
    window, max_req = limiter._get_limits(request, settings)
    assert window == 60
    assert max_req == 30


def test_limit_settings_chat() -> None:
    """chat 组限流配置：60 秒窗口，rate_limit_chat_per_minute。"""
    settings = Settings(rate_limit_chat_per_minute=20)
    limiter = RateLimiter()
    request = _make_request("/chat/stream")
    window, max_req = limiter._get_limits(request, settings)
    assert window == 60
    assert max_req == 20


def test_limit_settings_admin() -> None:
    """admin 组限流配置：60 秒窗口，rate_limit_admin_per_minute。"""
    settings = Settings(rate_limit_admin_per_minute=120)
    limiter = RateLimiter()
    request = _make_request("/api/admin/")
    window, max_req = limiter._get_limits(request, settings)
    assert window == 60
    assert max_req == 120


def test_limit_settings_auth() -> None:
    """auth 组限流配置：60 秒窗口，rate_limit_auth_per_minute。"""
    settings = Settings(rate_limit_auth_per_minute=10)
    limiter = RateLimiter()
    request = _make_request("/api/auth/login")
    window, max_req = limiter._get_limits(request, settings)
    assert window == 60
    assert max_req == 10


def test_limit_settings_health() -> None:
    """health 组限流配置：5 秒窗口，10 次请求。"""
    settings = Settings()
    limiter = RateLimiter()
    request = _make_request("/api/health")
    window, max_req = limiter._get_limits(request, settings)
    assert window == 5
    assert max_req == 10


def test_rate_limiter_disabled() -> None:
    """rate_limit_enabled=False 时放行所有请求。"""
    settings = Settings(rate_limit_enabled=False)
    limiter = RateLimiter()
    request = _make_request("/api/guest/")
    with (
        patch("app.security.rate_limiter.get_settings", return_value=settings),
        patch.object(limiter, "_get_redis", wraps=limiter._get_redis) as mock_get,
    ):
        result = asyncio_run(limiter.check(request))
        assert result is True
        mock_get.assert_not_called()


def test_rate_limiter_redis_unavailable() -> None:
    """Redis 不可达时放行（fail-open）。"""
    settings = Settings(rate_limit_enabled=True)
    limiter = RateLimiter()
    with (
        patch("app.security.rate_limiter.get_settings", return_value=settings),
        patch.object(limiter, "_get_redis", return_value=None),
    ):
        request = _make_request("/api/guest/")
        result = asyncio_run(limiter.check(request))
        assert result is True


def test_middleware_does_not_block_when_redis_unavailable() -> None:
    """中间件在 Redis 不可用时不会阻塞正常请求（fail-open）。"""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200


def test_middleware_guest_route_ok() -> None:
    """中间件不阻拦 /api/guest/ 路径。"""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/api/guest/rooms")
    # 即使 404（无路由），中间件也不应返回 429
    assert resp.status_code in (200, 401, 404)


def _make_request(
    path: str,
    client_host: str = "127.0.0.1",
    headers: dict[str, str] | None = None,
) -> Request:
    """构造一个模拟的 Request 对象。"""
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (client_host, 54321),
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
        "root_path": "",
    }
    return Request(scope)


def asyncio_run(coro):
    """在同步测试中运行协程。"""
    import asyncio
    return asyncio.run(coro)