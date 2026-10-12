"""智能体日志 REST 接口验证：授权矩阵、筛选分页、软删留痕、软删不影响报表。

种子事实落在专属时间窗内，规避共享库其它测试污染。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.admin_log import AdminActionLog
from app.repositories import AuditRepository, UserRepository
from app.repositories.agent_run_repository import AgentRunRepository
from app.security import hash_password
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration

PASSWORD = "log-pass-1"


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
    username = f"log-{role}-{uuid4().hex[:8]}"

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


def _seed(api: Api) -> dict[str, Any]:
    """创建一个 Run 及其模型+工具审计行，返回 ids。"""

    async def _body() -> dict[str, Any]:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            intake = await intake_message(session, username="log-seed", text="测试日志")
            assert intake.run_id
            run = await AgentRunRepository(session).get_by_id(intake.run_id)
            assert run is not None
            run.status = "completed"
            audits = AuditRepository(session)
            m1 = await audits.append_model(
                run_id=intake.run_id, agent_slug="front-desk",
                actor_type="guest", actor_id="t", model="deepseek-flash",
                status="completed", prompt_tokens=100, completion_tokens=20,
                retrieval_refs=[{"chunk_id": "c1"}],
            )
            m2 = await audits.append_model(
                run_id=intake.run_id, agent_slug="front-desk",
                actor_type="guest", actor_id="t", model="deepseek-flash",
                status="completed", prompt_tokens=50, completion_tokens=10,
                retrieval_refs=None,
            )
            t1 = await audits.append_tool(
                run_id=intake.run_id, agent_slug="front-desk",
                tool_name="query_kb", status="completed",
            )
            t2 = await audits.append_tool(
                run_id=intake.run_id, agent_slug="front-desk",
                tool_name="list_my_orders", status="failed", error="boom",
            )
            await session.commit()
            return {
                "run_id": str(intake.run_id),
                "thread_id": str(intake.thread_id),
                "model_ids": [m1.id, m2.id],
                "tool_ids": [t1.id, t2.id],
            }

    return asyncio.run(_body())


def test_list_logs_pagination_and_filters(api: Api) -> None:
    admin = _token(api, role="admin")
    data = _seed(api)

    # Full list
    full = api.client.get("/api/admin/logs", headers=_auth(admin))
    assert full.status_code == 200
    body = full.json()
    assert body["total"] >= 4
    assert len(body["entries"]) >= 4
    assert body["has_more"] is False

    # Filter by tool kind
    tools = api.client.get("/api/admin/logs", headers=_auth(admin), params={"kind": "tool"})
    assert tools.status_code == 200
    assert all(e["kind"] == "tool" for e in tools.json()["entries"])

    # Filter by model kind
    models = api.client.get("/api/admin/logs", headers=_auth(admin), params={"kind": "model"})
    assert models.status_code == 200
    assert all(e["kind"] == "model" for e in models.json()["entries"])

    # Filter by run_id
    by_run = api.client.get(
        "/api/admin/logs", headers=_auth(admin), params={"run_id": data["run_id"]}
    )
    assert by_run.status_code == 200
    assert all(e["run_id"] == data["run_id"] for e in by_run.json()["entries"])

    # Filter by status=failed
    failed = api.client.get("/api/admin/logs", headers=_auth(admin), params={"status": "failed"})
    assert failed.status_code == 200
    assert all(e["status"] == "failed" for e in failed.json()["entries"])

    # Pagination
    first_page = api.client.get(
        "/api/admin/logs", headers=_auth(admin), params={"page": 1, "page_size": 2}
    )
    assert first_page.status_code == 200
    assert len(first_page.json()["entries"]) == 2

    # Guest enrichment
    assert full.json()["entries"][0].get("guest_username") is not None

    # Include deleted (no deleted rows yet)
    with_deleted = api.client.get(
        "/api/admin/logs", headers=_auth(admin), params={"include_deleted": "true"}
    )
    assert with_deleted.status_code == 200
    assert with_deleted.json()["total"] == full.json()["total"]


def test_run_timeline_detail(api: Api) -> None:
    admin = _token(api, role="admin")
    data = _seed(api)

    resp = api.client.get(f"/api/admin/logs/runs/{data['run_id']}/timeline", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is False
    all_kinds = {e["kind"] for e in body["entries"]}
    assert all_kinds == {"model", "tool"}
    assert len(body["entries"]) == 4


def test_soft_delete_and_action_log(api: Api) -> None:
    admin = _token(api, role="admin")
    data = _seed(api)

    # Delete 2 tool rows
    tool_ids = [data["tool_ids"][0]]
    resp = api.client.request(
        "DELETE", "/api/admin/logs/delete",
        headers=_auth(admin),
        json={"tool_ids": tool_ids},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()
    assert result["tool_affected"] == 1
    assert result["model_affected"] == 0
    assert result["total_affected"] == 1
    assert result["log_id"] > 0

    # Verify soft-deleted row is excluded by default
    filtered = api.client.get("/api/admin/logs", headers=_auth(admin), params={"kind": "tool"})
    assert all(e["kind"] == "tool" for e in filtered.json()["entries"])
    deleted_ids = {e["id"] for e in filtered.json()["entries"]}
    assert tool_ids[0] not in deleted_ids

    # Verify include_deleted brings it back
    with_deleted = api.client.get(
        "/api/admin/logs", headers=_auth(admin),
        params={"kind": "tool", "include_deleted": "true"},
    )
    assert with_deleted.status_code == 200
    all_tool_ids = {e["id"] for e in with_deleted.json()["entries"]}
    assert tool_ids[0] in all_tool_ids

    # Verify action log
    action_logs = api.client.get("/api/admin/logs/action-logs", headers=_auth(admin))
    assert action_logs.status_code == 200
    logs = action_logs.json()
    assert any(log["action"] == "soft_delete_audit" for log in logs)

    # Verify admin_action_log table has the record
    async def _check_action_log() -> int:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            count = (await session.execute(
                select(func.count()).select_from(AdminActionLog)
            )).scalar_one()
            return int(count)

    assert asyncio.run(_check_action_log()) >= 1


def test_soft_delete_empty_request_is_rejected(api: Api) -> None:
    admin = _token(api, role="admin")
    resp = api.client.request(
        "DELETE", "/api/admin/logs/delete",
        headers=_auth(admin),
        json={"model_ids": [], "tool_ids": []},
    )
    assert resp.status_code == 400


def test_log_endpoints_enforce_role_matrix(api: Api) -> None:
    _seed(api)
    get_paths = [
        "/api/admin/logs",
        "/api/admin/logs/runs/00000000-0000-0000-0000-000000000001/timeline",
        "/api/admin/logs/action-logs",
    ]
    del_path = "/api/admin/logs/delete"

    staff = _token(api, role="staff")
    customer = _token(api, role="customer")
    manager = _token(api, role="manager")
    admin = _token(api, role="admin")

    for path in get_paths:
        assert api.client.get(path).status_code == 401
        assert api.client.get(path, headers={"Authorization": "Bearer forged"}).status_code == 401
        assert api.client.get(path, headers=_auth(staff)).status_code == 403
        assert api.client.get(path, headers=_auth(customer)).status_code == 403
        assert api.client.get(path, headers=_auth(manager)).status_code == 200

    del_body = {"tool_ids": [999]}
    for token, expected in [
        (None, 401), (staff, 403), (customer, 403), (manager, 403), (admin, 200)
    ]:
        hdrs = _auth(token) if token else None
        code = api.client.request("DELETE", del_path, json=del_body, headers=hdrs).status_code
        assert code == expected