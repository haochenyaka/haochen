"""认证流程集成测试（真实 PostgreSQL + Redis）：登录、令牌会话、禁用即时失效、失败关闭。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.repositories import UserRepository
from app.security import SessionService, hash_password

pytestmark = pytest.mark.integration


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


def _seed_account(
    api: Api,
    *,
    username: str,
    password: str,
    role: str,
    status: str = "active",
) -> None:
    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(password),
                role=role,
                display_name=f"{role}-{username}",
                status=status,
            )
            await session.commit()

    asyncio.run(_body())


def _set_status(api: Api, *, username: str, status: str) -> None:
    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            users = UserRepository(session)
            user = await users.get_by_username(username)
            assert user is not None
            await users.update_fields(user.id, {"status": status})
            await session.commit()

    asyncio.run(_body())


def _login(api: Api, *, username: str, password: str) -> dict[str, object]:
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_login_me_logout_lifecycle(api: Api) -> None:
    username = f"admin-{uuid4().hex[:8]}"
    _seed_account(api, username=username, password="pass-皓晨-1", role="admin")

    payload = _login(api, username=username, password="pass-皓晨-1")
    token = str(payload["token"])
    assert payload["expires_in"] == 43200
    user = dict(payload["user"])  # type: ignore[arg-type]
    assert user["username"] == username
    assert user["role"] == "admin"
    assert "password" not in str(payload)

    me = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == username
    assert me.json()["role"] == "admin"
    assert me.json()["via"] == "session"

    logout = api.client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout.status_code == 204

    after = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert after.status_code == 401


def test_login_rejects_bad_credentials_and_disabled_account(api: Api) -> None:
    username = f"manager-{uuid4().hex[:8]}"
    _seed_account(api, username=username, password="pass-1", role="manager")
    disabled = f"disabled-{uuid4().hex[:8]}"
    _seed_account(api, username=disabled, password="pass-1", role="manager", status="disabled")

    wrong = api.client.post(
        "/api/auth/login", json={"username": username, "password": "wrong-password"}
    )
    assert wrong.status_code == 401
    assert "密码" in wrong.json()["detail"]

    unknown = api.client.post(
        "/api/auth/login", json={"username": f"ghost-{uuid4().hex[:8]}", "password": "pass-1"}
    )
    assert unknown.status_code == 401

    blocked = api.client.post("/api/auth/login", json={"username": disabled, "password": "pass-1"})
    assert blocked.status_code == 403


def test_missing_or_forged_token_never_falls_back_to_dev_header(api: Api) -> None:
    anonymous = api.client.get("/api/auth/me")
    assert anonymous.status_code == 401

    forged = api.client.get("/api/auth/me", headers={"Authorization": "Bearer forged-token"})
    assert forged.status_code == 401

    both = api.client.get(
        "/api/auth/me",
        headers={
            "Authorization": "Bearer forged-token",
            "X-Dev-Username": f"dev-{uuid4().hex[:8]}",
        },
    )
    assert both.status_code == 401


def test_dev_header_seam_still_serves_guest_and_staff_development(api: Api) -> None:
    username = f"dev-staff-{uuid4().hex[:8]}"
    response = api.client.get("/api/auth/me", headers={"X-Dev-Username": username})
    assert response.status_code == 200
    assert response.json()["username"] == username
    assert response.json()["role"] == "customer"
    assert response.json()["via"] == "dev-header"


def test_disabled_account_loses_existing_session_immediately(api: Api) -> None:
    username = f"operator-{uuid4().hex[:8]}"
    _seed_account(api, username=username, password="pass-1", role="operator")
    token = str(_login(api, username=username, password="pass-1")["token"])

    ok = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200

    _set_status(api, username=username, status="disabled")
    revoked = api.client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert revoked.status_code == 401


def test_login_reports_503_when_session_backend_unavailable(api: Api) -> None:
    username = f"session-down-{uuid4().hex[:8]}"
    _seed_account(api, username=username, password="pass-1", role="admin")
    original = app.state.session_service
    app.state.session_service = SessionService(None)
    try:
        response = api.client.post(
            "/api/auth/login", json={"username": username, "password": "pass-1"}
        )
        assert response.status_code == 503
        me = api.client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer any"},
        )
        assert me.status_code == 401
    finally:
        app.state.session_service = original
