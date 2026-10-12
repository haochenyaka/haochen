"""会话读取与反馈 HTTP 级验证：历史含引用、评分改评撤销、转人工幂等与越权。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.audit import ModelAudit
from app.models.conversation import HandoffRequest, MessageFeedback
from app.models.runtime import Message
from app.routers.chat_router import get_run_dispatcher
from tests.integration.db_utils import run_with_session

pytestmark = pytest.mark.integration

CITATION_OLD: dict[str, object] = {
    "chunk_id": "0d3f2c66-1111-4a3e-9f01-000000000001",
    "document_id": "0d3f2c66-2222-4a3e-9f01-000000000002",
    "document_title": "样例·退房与延迟退房",
    "ordinal": 0,
    "score": 0.71,
    "recall_score": 0.62,
}
CITATION_NEW: dict[str, object] = {
    "chunk_id": "0d3f2c66-3333-4a3e-9f01-000000000003",
    "document_id": "0d3f2c66-4444-4a3e-9f01-000000000004",
    "document_title": "样例·早餐与餐饮",
    "ordinal": 1,
    "score": 0.93,
    "recall_score": 0.81,
}


class NoopDispatcher:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def enqueue_run(self, run_id: UUID) -> None:
        self.calls.append(run_id)

    async def aclose(self) -> None:
        return None


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
    app.dependency_overrides[get_run_dispatcher] = NoopDispatcher
    try:
        with TestClient(app) as client:
            yield Api(
                client=client,
                engine=engine,
                db_url=runtime_db_url,
                headers={"X-Dev-Username": f"conv-{uuid4().hex[:10]}"},
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _post_message(api: Api, text: str, *, username: str | None = None) -> dict[str, object]:
    headers = {**api.headers}
    if username is not None:
        headers["X-Dev-Username"] = username
    response = api.client.post(
        "/api/guest/chat/messages",
        json={"content": {"text": text}, "agent_slug": "front-desk"},
        headers=headers,
    )
    assert response.status_code == 201
    return response.json()


def _seed_reply(
    api: Api, *, thread_id: UUID, run_id: str, text: str, refs: list[dict[str, object]] | None
) -> UUID:
    async def _body(session: AsyncSession) -> UUID:
        message = Message(
            thread_id=thread_id, role="assistant", content={"text": text}, run_id=run_id
        )
        session.add(message)
        session.add(
            ModelAudit(
                run_id=run_id,
                seq=1,
                agent_slug="front-desk",
                actor_type="agent",
                actor_id="front-desk",
                model="stub-model",
                status="succeeded",
                retrieval_refs=refs,
            )
        )
        await session.flush()
        await session.commit()
        return message.id

    return run_with_session(api.db_url, _body)


def _append_audit(api: Api, *, run_id: str, seq: int, refs: list[dict[str, object]] | None) -> None:
    async def _body(session: AsyncSession) -> None:
        session.add(
            ModelAudit(
                run_id=run_id,
                seq=seq,
                agent_slug="front-desk",
                actor_type="agent",
                actor_id="front-desk",
                model="stub-model",
                status="succeeded",
                retrieval_refs=refs,
            )
        )
        await session.commit()

    run_with_session(api.db_url, _body)


def _count(db_url: str, model: Any) -> int:
    async def _body(session: AsyncSession) -> int:
        stmt = select(func.count()).select_from(model)
        return int((await session.execute(stmt)).scalar_one())

    return run_with_session(db_url, _body)


def _get_messages(api: Api, thread_id: UUID, *, limit: int | None = None) -> list[dict]:
    params = {} if limit is None else {"limit": limit}
    response = api.client.get(
        f"/api/guest/chat/threads/{thread_id}/messages", headers=api.headers, params=params
    )
    assert response.status_code == 200
    return response.json()["messages"]


def _put_feedback(api: Api, message_id: UUID, rating: str | None) -> int:
    response = api.client.put(
        f"/api/guest/chat/messages/{message_id}/feedback",
        json={"rating": rating},
        headers=api.headers,
    )
    return response.status_code


def test_history_carries_citations_and_runs_last_refs(api: Api) -> None:
    submission = _post_message(api, "早餐几点结束？")
    thread_id = UUID(str(submission["thread_id"]))
    run_id = str(submission["run_id"])
    reply_id = _seed_reply(
        api, thread_id=thread_id, run_id=run_id, text="早餐 7:00 至 10:00。", refs=[CITATION_OLD]
    )
    _append_audit(api, run_id=run_id, seq=2, refs=[CITATION_NEW])

    messages = _get_messages(api, thread_id)
    assert [item["role"] for item in messages] == ["user", "assistant"]
    user_message, reply = messages
    assert user_message["text"] == "早餐几点结束？"
    assert user_message["citations"] == []
    assert user_message["my_rating"] is None

    assert reply["message_id"] == str(reply_id)
    assert reply["text"] == "早餐 7:00 至 10:00。"
    assert reply["run_id"] == run_id
    assert reply["citations"] == [CITATION_NEW]

    limited = _get_messages(api, thread_id, limit=1)
    assert [item["message_id"] for item in limited] == [str(reply_id)]


def test_reply_without_refs_has_empty_citations(api: Api) -> None:
    submission = _post_message(api, "随便问问")
    thread_id = UUID(str(submission["thread_id"]))
    _seed_reply(
        api,
        thread_id=thread_id,
        run_id=str(submission["run_id"]),
        text="我看看。",
        refs=None,
    )

    messages = _get_messages(api, thread_id)
    assert messages[-1]["citations"] == []


def test_feedback_upsert_clear_and_targets(api: Api) -> None:
    submission = _post_message(api, "能延迟退房吗？")
    thread_id = UUID(str(submission["thread_id"]))
    user_message_id = UUID(str(submission["message_id"]))
    reply_id = _seed_reply(
        api,
        thread_id=thread_id,
        run_id=str(submission["run_id"]),
        text="可以延到 14:00。",
        refs=[CITATION_OLD],
    )

    assert _put_feedback(api, reply_id, "up") == 200
    messages = _get_messages(api, thread_id)
    assert messages[-1]["my_rating"] == "up"
    assert messages[0]["my_rating"] is None

    response = api.client.put(
        f"/api/guest/chat/messages/{reply_id}/feedback",
        json={"rating": "down", "comment": "答非所问"},
        headers=api.headers,
    )
    assert response.status_code == 200 and response.json()["rating"] == "down"
    assert _count(api.db_url, MessageFeedback) == 1
    assert _get_messages(api, thread_id)[-1]["my_rating"] == "down"

    assert _put_feedback(api, reply_id, None) == 200
    assert _count(api.db_url, MessageFeedback) == 0
    assert _get_messages(api, thread_id)[-1]["my_rating"] is None

    assert _put_feedback(api, user_message_id, "up") == 409
    assert _put_feedback(api, uuid4(), "up") == 404
    response = api.client.put(
        f"/api/guest/chat/messages/{reply_id}/feedback",
        json={"rating": "up"},
        headers={"X-Dev-Username": "conv-intruder"},
    )
    assert response.status_code == 404
    response = api.client.put(
        f"/api/guest/chat/messages/{reply_id}/feedback",
        json={"rating": "sideways"},
        headers=api.headers,
    )
    assert response.status_code == 422


def test_handoff_idempotent_and_ownership(api: Api) -> None:
    submission = _post_message(api, "我要找人工")
    thread_id = UUID(str(submission["thread_id"]))

    response = api.client.get(f"/api/guest/chat/threads/{thread_id}/handoff", headers=api.headers)
    assert response.status_code == 200
    assert response.json()["status"] == "none"
    assert response.json()["request_id"] is None

    response = api.client.post(
        f"/api/guest/chat/threads/{thread_id}/handoff",
        json={"reason": "需要人工确认押金"},
        headers=api.headers,
    )
    assert response.status_code == 201
    first = response.json()
    assert first["status"] == "pending"
    assert first["reason"] == "需要人工确认押金"
    assert first["created_at"] is not None

    response = api.client.post(
        f"/api/guest/chat/threads/{thread_id}/handoff",
        json={"reason": "再点一次"},
        headers=api.headers,
    )
    assert response.status_code == 200
    assert response.json()["request_id"] == first["request_id"]
    assert response.json()["reason"] == "需要人工确认押金"
    assert _count(api.db_url, HandoffRequest) == 1

    response = api.client.get(f"/api/guest/chat/threads/{thread_id}/handoff", headers=api.headers)
    assert response.status_code == 200
    assert response.json()["request_id"] == first["request_id"]
    assert response.json()["status"] == "pending"

    stranger = {"X-Dev-Username": "conv-intruder"}
    response = api.client.post(
        f"/api/guest/chat/threads/{thread_id}/handoff", json={}, headers=stranger
    )
    assert response.status_code == 403
    assert (
        api.client.get(
            f"/api/guest/chat/threads/{uuid4()}/handoff", headers=api.headers
        ).status_code
        == 404
    )
    assert (
        api.client.get(
            f"/api/guest/chat/threads/{uuid4()}/messages", headers=api.headers
        ).status_code
        == 404
    )
    assert (
        api.client.get(
            f"/api/guest/chat/threads/{thread_id}/messages", headers=stranger
        ).status_code
        == 403
    )
