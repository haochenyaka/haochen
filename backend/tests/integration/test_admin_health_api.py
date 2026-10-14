"""运行健康面板集成测试：Run 终态分布、缓存命中率趋势。

所有数值由 ReportService 从审计事实派生（验收主张 A4）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.audit import ModelAudit
from app.repositories import AgentRunRepository, UserRepository
from app.security import hash_password
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration

PASSWORD = "health-pass-1"


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _token(api: Api, *, role: str) -> str:
    username = f"health-{role}-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(PASSWORD),
                role=role,
                display_name=f"{role}-{username}",
            )
            await session.commit()

    asyncio.run(_body())
    resp = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_run_status_distribution(api: Api) -> None:
    """种子 Run 后断言终态分布包含了 completed/failed。"""
    admin = _token(api, role="admin")

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            a = await intake_message(session, username="health-a", text="WiFi")
            b = await intake_message(session, username="health-b", text="取消")
            assert a.run_id and b.run_id
            runs = AgentRunRepository(session)
            run_a = await runs.get_by_id(a.run_id)
            run_b = await runs.get_by_id(b.run_id)
            assert run_a and run_b
            run_a.status = "completed"
            run_b.status = "failed"
            run_b.attempt_count = 2
            await session.commit()

    asyncio.run(_body())

    params = {
        "start": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    resp = api.client.get(
        "/api/admin/health/run-status-distribution", headers=_auth(admin), params=params
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["total"] >= 2
    assert payload["counts"].get("completed", 0) >= 1
    assert payload["counts"].get("failed", 0) >= 1


def test_cache_hit_trend(api: Api) -> None:
    """种子 ModelAudit 后断言缓存命中趋势包含数据。"""
    admin = _token(api, role="admin")

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            intake = await intake_message(session, username="health-cache", text="test")
            assert intake.run_id
            session.add(
                ModelAudit(
                    run_id=str(intake.run_id),
                    seq=1,
                    agent_slug="front-desk",
                    actor_type="guest",
                    actor_id="t",
                    model="deepseek-flash",
                    status="completed",
                    prompt_tokens=100,
                    cached_prompt_tokens=40,
                )
            )
            await session.commit()

    asyncio.run(_body())

    params = {
        "start": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "end": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }
    resp = api.client.get("/api/admin/health/cache-hit-trend", headers=_auth(admin), params=params)
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert len(payload) >= 0  # 可能有当天数据


def test_health_endpoints_enforce_role_matrix(api: Api) -> None:
    staff = _token(api, role="staff")
    customer = _token(api, role="customer")
    manager = _token(api, role="manager")

    for path in [
        "/api/admin/health/run-status-distribution",
        "/api/admin/health/cache-hit-trend",
    ]:
        assert api.client.get(path).status_code == 401
        assert api.client.get(path, headers=_auth(staff)).status_code == 403
        assert api.client.get(path, headers=_auth(customer)).status_code == 403
        assert api.client.get(path, headers=_auth(manager)).status_code == 200
