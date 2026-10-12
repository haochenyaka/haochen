"""接入用例 HTTP 级验证：FIFO、request_id 幂等与冲突、取消接力、提交后才投递。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.runtime import AgentRun, Message, Request
from app.routers.chat_router import get_run_dispatcher
from tests.integration.db_utils import run_with_session

pytestmark = pytest.mark.integration


class RecordingDispatcher:
    """记录投递，并在独立连接上记录 Run 可见状态（证明投递发生在提交之后）。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.calls: list[UUID] = []
        self.committed_statuses: list[str | None] = []
        self.fail_next = False

    async def enqueue_run(self, run_id: UUID) -> None:
        async with AsyncSession(self._engine, expire_on_commit=False) as session:
            run = await session.get(AgentRun, run_id)
            self.committed_statuses.append(None if run is None else run.status)
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("simulated redis outage")
        self.calls.append(run_id)

    async def aclose(self) -> None:
        return None


@dataclass
class Api:
    client: TestClient
    dispatcher: RecordingDispatcher
    engine: AsyncEngine
    db_url: str
    headers: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    recorder = RecordingDispatcher(engine)
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_run_dispatcher] = lambda: recorder
    try:
        with TestClient(app) as client:
            yield Api(
                client=client,
                dispatcher=recorder,
                engine=engine,
                db_url=runtime_db_url,
                headers={"X-Dev-Username": f"intake-{uuid4().hex[:10]}"},
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _post_message(
    api: Api,
    text: str,
    *,
    thread_id: UUID | None = None,
    request_id: UUID | None = None,
    policy: str = "enqueue",
    username: str | None = None,
) -> tuple[int, dict[str, object]]:
    body: dict[str, object] = {
        "content": {"text": text},
        "agent_slug": "front-desk",
        "policy": policy,
    }
    if thread_id is not None:
        body["thread_id"] = str(thread_id)
    if request_id is not None:
        body["request_id"] = str(request_id)
    headers = {**api.headers}
    if username is not None:
        headers["X-Dev-Username"] = username
    response = api.client.post("/api/guest/chat/messages", json=body, headers=headers)
    return response.status_code, response.json() if response.content else {}


def _fetch_request(db_url: str, request_id: UUID) -> Request | None:
    async def _body(session: AsyncSession) -> Request | None:
        return await session.get(Request, request_id)

    return run_with_session(db_url, _body)


def _fetch_run(db_url: str, run_id: UUID) -> AgentRun | None:
    async def _body(session: AsyncSession) -> AgentRun | None:
        return await session.get(AgentRun, run_id)

    return run_with_session(db_url, _body)


def _count_thread_messages(db_url: str, thread_id: UUID) -> int:
    async def _body(session: AsyncSession) -> int:
        stmt = select(func.count()).select_from(Message).where(Message.thread_id == thread_id)
        return int((await session.execute(stmt)).scalar_one())

    return run_with_session(db_url, _body)


def test_submit_persists_then_dispatches(api: Api) -> None:
    status_code, body = _post_message(api, "你好")
    assert status_code == 201
    assert body["deduplicated"] is False
    assert body["queue_position"] is None
    run_id = UUID(str(body["run_id"]))

    assert api.dispatcher.calls == [run_id]
    assert api.dispatcher.committed_statuses == ["pending"]

    request = _fetch_request(api.db_url, UUID(str(body["request_id"])))
    assert request is not None and request.status == "dispatched"
    assert _count_thread_messages(api.db_url, UUID(str(body["thread_id"]))) == 1


def test_request_id_idempotency_and_conflicts(api: Api) -> None:
    request_id = uuid4()
    status_code, first = _post_message(api, "第一次", request_id=request_id)
    assert status_code == 201
    assert len(api.dispatcher.calls) == 1

    status_code, second = _post_message(api, "重复提交", request_id=request_id)
    assert status_code == 200
    assert second["deduplicated"] is True
    assert second["request_id"] == first["request_id"]
    assert second["message_id"] == first["message_id"]
    assert second["thread_id"] == first["thread_id"]
    assert len(api.dispatcher.calls) == 1
    assert _count_thread_messages(api.db_url, UUID(str(first["thread_id"]))) == 1

    status_code, _ = _post_message(api, "跨用户", request_id=request_id, username="intake-other")
    assert status_code == 409

    status_code, _ = _post_message(
        api, "跨线程", request_id=request_id, thread_id=UUID(str(uuid4()))
    )
    assert status_code in (404, 409)


def test_thread_fifo_queue_and_cancel_relay(api: Api) -> None:
    status_code, first = _post_message(api, "第一句")
    assert status_code == 201
    thread_id = UUID(str(first["thread_id"]))
    run1 = UUID(str(first["run_id"]))

    status_code, second = _post_message(api, "第二句", thread_id=thread_id)
    assert status_code == 201
    assert second["run_id"] is None
    assert second["queue_position"] == 1

    status_code, third = _post_message(api, "第三句", thread_id=thread_id)
    assert status_code == 201
    assert third["queue_position"] == 2
    assert len(api.dispatcher.calls) == 1

    response = api.client.get(
        f"/api/guest/chat/requests/{second['request_id']}", headers=api.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert response.json()["queue_position"] == 1
    assert response.json()["run_id"] is None

    response = api.client.post(
        f"/api/guest/chat/requests/{second['request_id']}/cancel", headers=api.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    response = api.client.post(
        f"/api/guest/chat/requests/{first['request_id']}/cancel", headers=api.headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    assert len(api.dispatcher.calls) == 2
    run3 = api.dispatcher.calls[1]

    third_request = _fetch_request(api.db_url, UUID(str(third["request_id"])))
    assert third_request is not None and third_request.status == "dispatched"
    run = _fetch_run(api.db_url, run3)
    assert run is not None and run.status == "pending"
    assert str(run.request_id) == third["request_id"]
    assert _fetch_run(api.db_url, run1).status == "cancelled"  # type: ignore[union-attr]


def test_reject_policy_does_not_queue(api: Api) -> None:
    status_code, first = _post_message(api, "先占线程")
    assert status_code == 201
    thread_id = UUID(str(first["thread_id"]))

    status_code, _ = _post_message(api, "同步调用", thread_id=thread_id, policy="reject")
    assert status_code == 409
    assert _count_thread_messages(api.db_url, thread_id) == 1

    status_code, fresh = _post_message(api, "空线程同步调用", policy="reject")
    assert status_code == 201
    assert fresh["run_id"] is not None


def test_thread_boundaries_return_404_or_403(api: Api) -> None:
    status_code, first = _post_message(api, "建线程")
    assert status_code == 201
    thread_id = UUID(str(first["thread_id"]))

    status_code, _ = _post_message(api, "未知线程", thread_id=uuid4())
    assert status_code == 404

    status_code, _ = _post_message(api, "越权线程", thread_id=thread_id, username="intake-other")
    assert status_code == 403

    response = api.client.get(f"/api/guest/chat/requests/{uuid4()}", headers=api.headers)
    assert response.status_code == 404

    response = api.client.get(
        f"/api/guest/chat/requests/{first['request_id']}",
        headers={"X-Dev-Username": "intake-other"},
    )
    assert response.status_code == 404


def test_dispatch_failure_keeps_run_pending(api: Api) -> None:
    api.dispatcher.fail_next = True
    status_code, body = _post_message(api, "投递失败")
    assert status_code == 201
    run_id = UUID(str(body["run_id"]))

    assert api.dispatcher.calls == []
    run = _fetch_run(api.db_url, run_id)
    assert run is not None and run.status == "pending"
    assert api.dispatcher.committed_statuses == ["pending"]
