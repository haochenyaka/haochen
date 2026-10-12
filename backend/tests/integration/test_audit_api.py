"""审计读接口验证：线程级时间线（倒序 + truncated）、归属校验、参数边界。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.repositories import AuditRepository, UserRepository
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine
    db_url: str
    headers: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(
                client=client,
                engine=engine,
                db_url=runtime_db_url,
                headers={"X-Dev-Username": f"audit-{uuid4().hex[:10]}"},
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _seed_thread_with_audits(api: Api, *, username: str, model_rows: int, tool_rows: int) -> str:
    async def _body() -> dict[str, str]:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            user = await UserRepository(session).get_or_create(username=username)
            intake = await intake_message(session, username=username)
            audits = AuditRepository(session)
            for index in range(model_rows):
                await audits.append_model(
                    run_id=intake.run_id or uuid4(),
                    agent_slug="front-desk",
                    actor_type="guest",
                    actor_id=str(user.id),
                    model="stub-model",
                    status="completed",
                    prompt_tokens=10 + index,
                    cached_prompt_tokens=index,
                    completion_tokens=5,
                    duration_ms=index,
                )
            for index in range(tool_rows):
                await audits.append_tool(
                    run_id=intake.run_id or uuid4(),
                    agent_slug="front-desk",
                    tool_name=f"tool-{index}",
                    status="completed",
                    args_digest={"n": index},
                    duration_ms=index,
                )
            await session.commit()
            return {"thread_id": str(intake.thread_id)}

    return asyncio.run(_body())["thread_id"]


def test_thread_audits_timeline_desc_and_truncated(api: Api) -> None:
    username = f"audit-reader-{uuid4().hex[:8]}"
    thread_id = _seed_thread_with_audits(api, username=username, model_rows=3, tool_rows=2)
    headers = {"X-Dev-Username": username}

    full = api.client.get(f"/api/guest/chat/threads/{thread_id}/audits", headers=headers)
    assert full.status_code == 200
    payload = full.json()
    assert payload["truncated"] is False
    assert len(payload["entries"]) == 5
    assert {entry["kind"] for entry in payload["entries"]} == {"model", "tool"}
    created = [entry["created_at"] for entry in payload["entries"]]
    assert created == sorted(created, reverse=True)
    model_entry = next(entry for entry in payload["entries"] if entry["kind"] == "model")
    assert model_entry["cached_prompt_tokens"] is not None
    assert model_entry["run_id"]
    tool_entry = next(entry for entry in payload["entries"] if entry["kind"] == "tool")
    assert tool_entry["args_digest"] is not None

    limited = api.client.get(f"/api/guest/chat/threads/{thread_id}/audits?limit=2", headers=headers)
    assert limited.status_code == 200
    limited_payload = limited.json()
    assert limited_payload["truncated"] is True
    assert [entry["id"] for entry in limited_payload["entries"]] == [
        entry["id"] for entry in payload["entries"][:2]
    ]


def test_thread_audits_scope_guards(api: Api) -> None:
    username = f"audit-owner-{uuid4().hex[:8]}"
    thread_id = _seed_thread_with_audits(api, username=username, model_rows=1, tool_rows=0)

    stranger = api.client.get(
        f"/api/guest/chat/threads/{thread_id}/audits",
        headers={"X-Dev-Username": f"audit-stranger-{uuid4().hex[:8]}"},
    )
    assert stranger.status_code == 403

    missing = api.client.get(
        f"/api/guest/chat/threads/{uuid4()}/audits", headers={"X-Dev-Username": username}
    )
    assert missing.status_code == 404

    invalid = api.client.get(
        f"/api/guest/chat/threads/{thread_id}/audits?limit=0", headers={"X-Dev-Username": username}
    )
    assert invalid.status_code == 422


def test_unknown_thread_identifier_is_rejected(api: Api) -> None:
    response = api.client.get(
        "/api/guest/chat/threads/not-a-uuid/audits", headers={"X-Dev-Username": "audit-any"}
    )
    assert response.status_code == 422
