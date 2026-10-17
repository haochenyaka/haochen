"""运营报表 REST 接口验证：授权矩阵、时间窗校验、数值 == 审计重算（A4 经 HTTP 层取证）。

种子事实落在专属时间窗内，规避共享库其它测试污染；软删不影响报表口径由 test_admin_logs_api 覆盖。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.audit import ModelAudit, ToolAudit
from app.models.conversation import HandoffRequest
from app.models.runtime import AgentRun, Message, Thread
from app.models.task import Task
from app.repositories import AgentRunRepository, HandoffRequestRepository, UserRepository
from app.security import hash_password
from app.services.report_service import ReportService
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration

PASSWORD = "report-pass-1"


@pytest.fixture(autouse=True)
def _isolate_datalake(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离外部数据湖：本文件断言 PostgreSQL 重算口径，预聚合读取由降级单测覆盖。"""

    async def _no_preaggregate(self: ReportService, table: str, sql: str) -> None:
        return None

    monkeypatch.setattr(ReportService, "_duckdb_query_agg", _no_preaggregate)


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
    """建号并登录，返回 Bearer 令牌。"""
    username = f"rep-{role}-{uuid4().hex[:8]}"

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
    response = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _unique_window() -> tuple[datetime, datetime, datetime]:
    base = datetime(2032, 1, 1, tzinfo=UTC) + timedelta(seconds=uuid4().int % (300 * 86400))
    return base, base + timedelta(minutes=10), base + timedelta(minutes=1)


async def _stamp(session: AsyncSession, thread_ids: list[UUID], stamp: datetime) -> None:
    for stmt in (
        update(Thread).where(Thread.id.in_(thread_ids)).values(created_at=stamp),
        update(Message).where(Message.thread_id.in_(thread_ids)).values(created_at=stamp),
        update(AgentRun).where(AgentRun.thread_id.in_(thread_ids)).values(created_at=stamp),
        update(HandoffRequest)
        .where(HandoffRequest.thread_id.in_(thread_ids))
        .values(created_at=stamp),
    ):
        await session.execute(stmt.execution_options(synchronize_session=False))


def _seed(api: Api) -> tuple[datetime, datetime]:
    """两线程/两 Run + 审计事实，全部落在专属窗口；返回 (start, end)。"""
    start, end, stamp = _unique_window()

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            a = await intake_message(session, username="api-rep-a", text="房间WiFi怎么用")
            b = await intake_message(session, username="api-rep-b", text="我要取消订单")
            assert a.run_id and b.run_id
            runs = AgentRunRepository(session)

            run_a = await runs.get_by_id(a.run_id)
            assert run_a is not None
            run_a.status = "completed"
            run_a.attempt_count = 1
            run_a.started_at = stamp
            run_a.first_token_at = stamp + timedelta(seconds=1)
            session.add(
                ModelAudit(
                    run_id=str(a.run_id),
                    seq=1,
                    agent_slug="front-desk",
                    actor_type="system",
                    actor_id="t",
                    model="deepseek-flash",
                    status="completed",
                    duration_ms=900,
                    prompt_tokens=100,
                    cached_prompt_tokens=40,
                    completion_tokens=20,
                    retrieval_refs=[{"chunk_id": "c1"}],
                    created_at=stamp,
                )
            )
            session.add(
                ToolAudit(
                    run_id=str(a.run_id),
                    seq=1,
                    agent_slug="front-desk",
                    tool_name="query_kb",
                    status="completed",
                    duration_ms=30,
                    created_at=stamp,
                )
            )

            run_b = await runs.get_by_id(b.run_id)
            assert run_b is not None
            run_b.status = "failed"
            run_b.attempt_count = 2
            run_b.started_at = stamp
            run_b.first_token_at = None
            await HandoffRequestRepository(session).create(
                thread_id=b.thread_id, user_id=run_b.user_id, reason="客人要求人工"
            )

            session.add(
                Task(
                    task_type="lock.rotate_daily",
                    handler_version=1,
                    payload={},
                    status="succeeded",
                    created_at=stamp,
                )
            )
            session.add(
                Task(
                    task_type="checkin.prepare",
                    handler_version=1,
                    payload={},
                    status="failed",
                    created_at=stamp,
                )
            )

            await _stamp(session, [a.thread_id, b.thread_id], stamp)
            await session.commit()

    asyncio.run(_body())
    return start, end


def _params(start: datetime, end: datetime) -> dict[str, str]:
    return {"start": start.isoformat(), "end": end.isoformat()}


def test_overview_values_equal_audit_recompute(api: Api) -> None:
    admin = _token(api, role="admin")
    start, end = _seed(api)

    response = api.client.get(
        "/api/admin/reports/overview", headers=_auth(admin), params=_params(start, end)
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["reception"]["threads"] == 2
    assert payload["reception"]["runs"] == 2
    assert payload["reception"]["messages"] >= 2

    res = payload["resolution"]
    assert res["terminal_runs"] == 2
    assert res["first_resolved_runs"] == 1
    assert res["first_resolution_rate"] == 0.5
    assert res["tasks_succeeded"] == 1 and res["tasks_failed"] == 1
    assert res["task_success_rate"] == 0.5

    assert payload["handoff"]["handoffs"] == 1
    assert payload["handoff"]["rate"] == 0.5

    rec = payload["recall"]
    assert rec["offline_recall_at_k"] is None
    assert rec["online_runs_with_retrieval"] == 1
    assert rec["online_runs_cited"] == 1
    assert rec["online_citation_rate"] == 1.0

    assert payload["cache"]["prompt_tokens"] == 100
    assert payload["cache"]["cached_prompt_tokens"] == 40
    assert payload["cache"]["hit_rate"] == 0.4
    assert payload["cache"]["estimated_saving"] == 0.0

    perf = payload["performance"]
    assert perf["ttft_samples"] == 1
    assert perf["ttft_p50_ms"] is not None
    assert perf["tool_calls"] == 1 and perf["tool_completed"] == 1
    assert perf["tool_success_rate"] == 1.0
    assert perf["runs_with_attempts"] == 2 and perf["runs_retried"] == 1
    assert perf["model_retry_rate"] == 0.5


def test_tokens_and_hot_questions_and_series_endpoints(api: Api) -> None:
    manager = _token(api, role="manager")
    start, end = _seed(api)
    params = _params(start, end)

    tokens = api.client.get("/api/admin/reports/tokens", headers=_auth(manager), params=params)
    assert tokens.status_code == 200, tokens.text
    body = tokens.json()
    assert body["totals"]["prompt_tokens"] == 100
    assert body["totals"]["completion_tokens"] == 20
    assert body["totals"]["cached_prompt_tokens"] == 40
    assert [bucket["key"] for bucket in body["by_model"]] == ["deepseek-flash"]
    assert body["by_model"][0]["prompt_tokens"] == 100

    hot = api.client.get(
        "/api/admin/reports/hot-questions", headers=_auth(manager), params={**params, "limit": 5}
    )
    assert hot.status_code == 200, hot.text
    tool_names = {item["name"] for item in hot.json()["top_tools"]}
    assert "query_kb" in tool_names

    series = api.client.get(
        "/api/admin/reports/reception-series", headers=_auth(manager), params=params
    )
    assert series.status_code == 200, series.text
    days = series.json()["series"]
    assert sum(item["runs"] for item in days) == 2
    assert sum(item["threads"] for item in days) == 2


def test_report_endpoints_enforce_role_matrix(api: Api) -> None:
    endpoints = [
        "/api/admin/reports/overview",
        "/api/admin/reports/tokens",
        "/api/admin/reports/hot-questions",
        "/api/admin/reports/reception-series",
    ]

    staff = _token(api, role="staff")
    customer = _token(api, role="customer")

    for path in endpoints:
        assert api.client.get(path).status_code == 401
        assert api.client.get(path, headers={"Authorization": "Bearer forged"}).status_code == 401
        assert api.client.get(path, headers=_auth(staff)).status_code == 403
        assert api.client.get(path, headers=_auth(customer)).status_code == 403
        assert api.client.get(path, headers=_auth(_token(api, role="manager"))).status_code == 200


def test_window_validation_rejects_bad_ranges(api: Api) -> None:
    admin = _token(api, role="admin")
    now = datetime.now(UTC)

    inverted = api.client.get(
        "/api/admin/reports/overview",
        headers=_auth(admin),
        params={"start": now.isoformat(), "end": (now - timedelta(days=1)).isoformat()},
    )
    assert inverted.status_code == 400

    too_wide = api.client.get(
        "/api/admin/reports/overview",
        headers=_auth(admin),
        params={
            "start": (now - timedelta(days=400)).isoformat(),
            "end": now.isoformat(),
        },
    )
    assert too_wide.status_code == 400
