"""MCP 服务器 REST 接口集成测试：CRUD、传输校验、连接测试、授权矩阵。

连接测试以替身注入（`probe_server` 打桩），不依赖真实 MCP gateway。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.agents.mcp.probe import PROBE_FAILED, PROBE_OK, ProbeResult
from app.db import get_session
from app.main import app
from app.repositories import UserRepository
from app.security import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "mcp-pass-1"


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine
    db_url: str


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine, db_url=runtime_db_url)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _token(api: Api, *, role: str) -> str:
    from uuid import uuid4

    username = f"mcp-{role}-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(PASSWORD),
                role=role,
                display_name=role,
            )
            await session.commit()

    asyncio.run(_body())
    resp = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create(api: Api, token: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "slug": "lock-mcp",
        "name": "门锁 MCP",
        "server_url": "http://mcp-gateway:9200/mcp",
        "transport": "streamable_http",
    }
    body.update(overrides)
    resp = api.client.post("/api/admin/mcp-servers", json=body, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_crud_roundtrip(api: Api) -> None:
    token = _token(api, role="admin")
    created = _create(api, token)
    assert created["transport"] == "streamable_http"
    assert created["last_status"] is None

    listed = api.client.get("/api/admin/mcp-servers", headers=_auth(token))
    assert listed.status_code == 200
    assert [s["slug"] for s in listed.json()] == ["lock-mcp"]

    upd = api.client.put(
        f"/api/admin/mcp-servers/{created['id']}",
        json={"name": "改名", "transport": "sse"},
        headers=_auth(token),
    )
    assert upd.status_code == 200
    assert upd.json()["name"] == "改名"
    assert upd.json()["transport"] == "sse"

    deleted = api.client.delete(f"/api/admin/mcp-servers/{created['id']}", headers=_auth(token))
    assert deleted.status_code == 204


def test_stdio_transport_rejected(api: Api) -> None:
    token = _token(api, role="admin")
    resp = api.client.post(
        "/api/admin/mcp-servers",
        json={
            "slug": "bad",
            "name": "bad",
            "server_url": "http://x/mcp",
            "transport": "stdio",
        },
        headers=_auth(token),
    )
    assert resp.status_code == 422
    assert "stdio" in resp.text


def test_test_connection_writes_probe_result(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _token(api, role="admin")
    created = _create(api, token)

    async def _fake_probe(**_kwargs: Any) -> ProbeResult:
        return ProbeResult(
            ok=True,
            status=PROBE_OK,
            tools=[{"name": "issue_credential", "description": "签发"}],
            latency_ms=12,
        )

    monkeypatch.setattr("app.services.mcp_server_service.probe_server", _fake_probe)

    resp = api.client.post(f"/api/admin/mcp-servers/{created['id']}/test", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["tool_count"] == 1

    # 探测结论落库，列表可见
    listed = api.client.get("/api/admin/mcp-servers", headers=_auth(token)).json()
    assert listed[0]["last_status"] == PROBE_OK
    assert listed[0]["discovered_tools"][0]["name"] == "issue_credential"

    tools = api.client.get(
        f"/api/admin/mcp-servers/{created['id']}/tools", headers=_auth(token)
    ).json()
    assert tools["tools"][0]["name"] == "issue_credential"


def test_test_connection_failure_recorded(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    token = _token(api, role="admin")
    created = _create(api, token)

    async def _fake_probe(**_kwargs: Any) -> ProbeResult:
        return ProbeResult(ok=False, status=PROBE_FAILED, error="ConnectionError: refused")

    monkeypatch.setattr("app.services.mcp_server_service.probe_server", _fake_probe)
    resp = api.client.post(f"/api/admin/mcp-servers/{created['id']}/test", headers=_auth(token))
    assert resp.status_code == 200
    assert resp.json()["ok"] is False

    listed = api.client.get("/api/admin/mcp-servers", headers=_auth(token)).json()
    assert listed[0]["last_status"] == PROBE_FAILED
    assert "refused" in (listed[0]["last_error"] or "")


def test_authorization_matrix(api: Api) -> None:
    admin = _token(api, role="admin")
    manager = _token(api, role="manager")
    staff = _token(api, role="staff")
    created = _create(api, admin)
    sid = created["id"]

    # 无 token → 401
    assert api.client.get("/api/admin/mcp-servers").status_code == 401
    # staff → 403
    assert api.client.get("/api/admin/mcp-servers", headers=_auth(staff)).status_code == 403
    # manager 读 200，写 403
    assert api.client.get("/api/admin/mcp-servers", headers=_auth(manager)).status_code == 200
    assert (
        api.client.post(
            "/api/admin/mcp-servers",
            json={"slug": "x", "name": "x", "server_url": "http://x"},
            headers=_auth(manager),
        ).status_code
        == 403
    )
    assert (
        api.client.post(f"/api/admin/mcp-servers/{sid}/test", headers=_auth(manager)).status_code
        == 403
    )
    # admin 写 200
    assert (
        api.client.delete(f"/api/admin/mcp-servers/{sid}", headers=_auth(admin)).status_code == 204
    )
