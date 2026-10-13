"""SSE 事件流验证：事件链与事实一致、断线续传、数据库终态投影、心跳与订阅鉴权。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID, uuid4

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.models import STUB_REPLY_PREFIX
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.main import app
from app.models.audit import ToolAudit
from app.models.runtime import Message, User
from app.repositories import AgentRunRepository, UserRepository
from app.routers.chat_router import (
    DEFAULT_DEV_USERNAME,
    get_run_stream_service,
    get_stream_guest,
)
from app.services.run_body import AgentRunBody
from app.services.run_events import (
    EVENT_INTERRUPT,
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_RUN_CANCELLED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_INTERRUPTED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
)
from app.services.run_stream import SSE_HEARTBEAT_COMMENT, RunStreamService
from app.services.run_worker import RunContext, RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import (
    RecordingDispatcher,
    RecordingRunEventBus,
    ScriptedToolChatModel,
    force_stub_model,
    intake_message,
)

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


async def _latest_reply_id(factory: SessionFactoryArg, thread_id: UUID) -> UUID | None:
    async with factory() as session:
        return (
            await session.execute(
                select(Message.id)
                .where(Message.thread_id == thread_id, Message.role == "assistant")
                .order_by(Message.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def test_run_emits_event_chain_matching_committed_facts(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件链顺序：认领后 run.started，回复整段回退为一次 delta，事实提交后再完成。"""
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username="stream-tester", text="有哪些房型")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)
        return {
            "outcome": outcome,
            "thread_id": str(intake.thread_id),
            "run_id": str(intake.run_id),
            "events": bus.records(intake.run_id),
            "reply_id": await _latest_reply_id(factory, intake.thread_id),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    records = result["events"]
    assert [record.event for record in records] == [
        EVENT_RUN_STARTED,
        EVENT_MESSAGE_DELTA,
        EVENT_MESSAGE_COMPLETED,
        EVENT_RUN_COMPLETED,
    ]
    assert [record.data["seq"] for record in records] == [1, 2, 3, 4]
    assert records[0].data["run_id"] == result["run_id"]
    assert records[0].data["thread_id"] == result["thread_id"]
    assert records[1].data["text"] == f"{STUB_REPLY_PREFIX}有哪些房型"
    # message.completed 携带已提交的回复消息 id：投影与事实可对账
    assert records[2].data["message_id"] == str(result["reply_id"])
    assert records[3].data["status"] == "completed"
    assert records[3].data["token_usage"]["model_calls"] == 1


def test_tool_loop_emits_call_and_result_events(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    executed: list[str] = []
    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def test_echo(text: str) -> str:
        """回显文本。"""
        executed.append(text)
        return f"echo:{text}"

    model = ScriptedToolChatModel(tool_name="test_echo")
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("test_echo",))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=InMemorySaver(),
                resource_policy=StaticResourcePolicy(allowed=frozenset({("tool", "test_echo")})),
                tool_registry=registry,
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username="stream-tool-tester", text="现在几点")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)
        return {
            "outcome": outcome,
            "events": bus.records(intake.run_id),
            "executed": list(executed),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["executed"] == ["你好"]
    records = result["events"]
    assert [record.event for record in records] == [
        EVENT_RUN_STARTED,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        EVENT_MESSAGE_DELTA,
        EVENT_MESSAGE_COMPLETED,
        EVENT_RUN_COMPLETED,
    ]
    call = records[1].data
    assert call["tool"] == "test_echo"
    assert call["call_id"] == "call-1"
    assert call["args"] == {"text": "你好"}
    tool_result = records[2].data
    assert tool_result["call_id"] == "call-1"
    assert tool_result["status"] == "completed"
    assert isinstance(tool_result["duration_ms"], int)
    assert tool_result["error"] is None
    assert records[3].data["text"] == "最终回复：echo:你好"


def test_stream_replays_and_resumes_from_last_event_id(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(
                session, username="stream-resume-tester", text="有哪些房型"
            )
            user = await UserRepository(session).get_or_create(username="stream-resume-tester")
        assert intake.run_id is not None
        await executor.execute(intake.run_id)

        service = RunStreamService(
            bus=bus, session_factory=factory, heartbeat_seconds=5.0, follow_block_ms=10
        )
        view = await service.authorize(run_id=intake.run_id, user_id=user.id)
        return {
            "ids": [record.id for record in bus.records(intake.run_id)],
            "full": await _collect(service.stream(run=view)),
            "resumed": await _collect(service.stream(run=view, last_event_id="2-0")),
            "non_cursor": await _collect(service.stream(run=view, last_event_id="projected-9")),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["ids"] == ["1-0", "2-0", "3-0", "4-0"]
    full = result["full"]
    assert len(full) == 4
    assert full[0].startswith("id: 1-0\nevent: run.started")
    assert full[-1].startswith("id: 4-0\nevent: run.completed")
    # 断线续传：只回放 Last-Event-ID 之后的事件；非游标 id 无法定位，从头回放
    assert [chunk.splitlines()[1] for chunk in result["resumed"]] == [
        "event: message.completed",
        "event: run.completed",
    ]
    assert result["non_cursor"] == full


def test_stream_projects_terminal_state_from_database(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """事件缺失（无总线/Redis 降级）时按数据库合成回复与终态，id 标记 projected。"""
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            intake = await intake_message(
                session, username="stream-projection-tester", text="有哪些房型"
            )
            user = await UserRepository(session).get_or_create(username="stream-projection-tester")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        bus = RecordingRunEventBus()
        service = RunStreamService(
            bus=bus, session_factory=factory, heartbeat_seconds=5.0, follow_block_ms=10
        )
        view = await service.authorize(run_id=intake.run_id, user_id=user.id)
        return {
            "outcome": outcome,
            "bus_empty": bus.records(intake.run_id) == [],
            "chunks": await _collect(service.stream(run=view)),
            "reply_id": await _latest_reply_id(factory, intake.thread_id),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["bus_empty"] is True
    chunks = result["chunks"]
    assert len(chunks) == 2
    assert chunks[0].startswith("id: projected-1\nevent: message.completed")
    assert '"projected": true' in chunks[0]
    assert f'"message_id": "{result["reply_id"]}"' in chunks[0]
    assert f'"text": "{STUB_REPLY_PREFIX}有哪些房型"' in chunks[0]
    assert chunks[1].startswith("id: projected-2\nevent: run.completed")
    assert '"status": "completed"' in chunks[1]


def test_stream_heartbeats_while_run_active(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> list[str]:
        async with factory() as session:
            intake = await intake_message(session, username="stream-heartbeat-tester")
            user = await UserRepository(session).get_or_create(username="stream-heartbeat-tester")
        assert intake.run_id is not None
        service = RunStreamService(
            bus=RecordingRunEventBus(),
            session_factory=factory,
            heartbeat_seconds=0.01,
            follow_block_ms=5,
        )
        view = await service.authorize(run_id=intake.run_id, user_id=user.id)
        stream = service.stream(run=view)
        chunks: list[str] = []
        try:
            async for chunk in stream:
                chunks.append(chunk)
                if chunk == SSE_HEARTBEAT_COMMENT:
                    break
        finally:
            await stream.aclose()
        return chunks

    chunks = run_with_session_factory(runtime_db_url, _body)
    # 运行未达终态：保持连接只发心跳注释，不杜撰事件
    assert chunks == [SSE_HEARTBEAT_COMMENT]


def test_stream_endpoint_guards_and_resumes(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    engine: AsyncEngine = create_async_engine(runtime_db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    bus = RecordingRunEventBus()

    async def _seed() -> str:
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username="stream-api-tester", text="有哪些房型")
        assert intake.run_id is not None
        assert await executor.execute(intake.run_id) == "completed"
        return str(intake.run_id)

    run_id = asyncio.run(_seed())

    async def _override_guest(
        x_dev_username: Annotated[str, Header(alias="X-Dev-Username")] = DEFAULT_DEV_USERNAME,
    ) -> User:
        async with factory() as session:
            user = await UserRepository(session).get_or_create(username=x_dev_username)
            await session.commit()
            return user

    def _override_service() -> RunStreamService:
        return RunStreamService(
            bus=bus, session_factory=factory, heartbeat_seconds=5.0, follow_block_ms=10
        )

    app.dependency_overrides[get_stream_guest] = _override_guest
    app.dependency_overrides[get_run_stream_service] = _override_service
    try:
        with TestClient(app) as client:
            headers = {"X-Dev-Username": "stream-api-tester"}
            ok = client.get(f"/api/guest/chat/stream?run_id={run_id}", headers=headers)
            resumed = client.get(
                f"/api/guest/chat/stream?run_id={run_id}",
                headers={**headers, "Last-Event-ID": "3-0"},
            )
            querystring = client.get(
                f"/api/guest/chat/stream?run_id={run_id}&last_event_id=3-0", headers=headers
            )
            unknown = client.get(f"/api/guest/chat/stream?run_id={uuid4()}", headers=headers)
            stranger = client.get(
                f"/api/guest/chat/stream?run_id={run_id}",
                headers={"X-Dev-Username": f"stream-stranger-{uuid4().hex[:8]}"},
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())

    assert ok.status_code == 200
    assert ok.headers["content-type"].startswith("text/event-stream")
    assert ok.headers["cache-control"] == "no-cache"
    assert "id: 1-0\nevent: run.started" in ok.text
    assert "event: run.completed" in ok.text
    # 断线续传：标准 Last-Event-ID 头与查询参数等价，终态事件仍会送达
    assert "event: run.started" not in resumed.text
    assert "event: run.completed" in resumed.text
    assert resumed.text == querystring.text
    assert unknown.status_code == 404
    assert stranger.status_code == 403


def test_approval_wait_emits_interrupt_and_suspends(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """危险工具进入审批等待：Run 挂起为 interrupted、发 interrupt 事件、不写工具事实。"""
    force_stub_model(monkeypatch)
    registry = ToolRegistry()

    @builtin_tool(category="test", dangerous=True, read_only=False, registry=registry)
    async def rotate_room_password(room: str) -> str:
        """危险操作：轮换房间门锁密码。"""
        return f"rotated:{room}"

    model = ScriptedToolChatModel(tool_name="rotate_room_password", tool_args={"room": "A101"})
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("rotate_room_password",))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=InMemorySaver(),
                resource_policy=StaticResourcePolicy(
                    allowed=frozenset({("tool", "rotate_room_password")})
                ),
                tool_registry=registry,
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username="stream-approval-tester", text="换密码")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)
        async with factory() as session:
            run = await AgentRunRepository(session).get_by_id(intake.run_id)
            tool_rows = (
                await session.execute(
                    select(func.count())
                    .select_from(ToolAudit)
                    .where(ToolAudit.run_id == str(intake.run_id))
                )
            ).scalar_one()
            assistant_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(
                        Message.thread_id == intake.thread_id,
                        Message.role == "assistant",
                    )
                )
            ).scalar_one()
        assert run is not None
        return {
            "outcome": outcome,
            "events": bus.records(intake.run_id),
            "run_status": run.status,
            "run_error": run.error,
            "tool_rows": tool_rows,
            "assistant_count": assistant_count,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    # 挂起：Run 转 interrupted（非终态、不写工具事实、无回复），发 interrupt 事件
    assert result["outcome"] == "interrupted"
    assert [record.event for record in result["events"]] == [
        EVENT_RUN_STARTED,
        EVENT_TOOL_CALL,
        EVENT_INTERRUPT,
        EVENT_RUN_INTERRUPTED,
    ]
    interrupt = result["events"][2].data
    assert interrupt["tool"] == "rotate_room_password"
    assert interrupt["call_id"] == "call-1"
    assert "审批" in interrupt["reason"]
    assert result["run_status"] == "interrupted"
    assert result["run_error"] is None
    assert result["tool_rows"] == 0
    assert result["assistant_count"] == 0


def test_cancel_intent_converges_to_cancelled_terminal_event(
    runtime_db_url: str,
) -> None:
    """取消以数据库为准：意图先落库，执行体复核后主动中止，终态投影 run.cancelled。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()

        async def body(ctx: RunContext) -> None:
            async with factory() as session:
                assert await AgentRunRepository(session).request_cancel(ctx.run_id) is True
                await session.commit()
            await ctx.ensure_not_cancelled()

        executor = RunExecutor(
            body,
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username="stream-cancel-tester", text="算了")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)
        async with factory() as session:
            run = await AgentRunRepository(session).get_by_id(intake.run_id)
        assert run is not None
        return {
            "outcome": outcome,
            "events": bus.records(intake.run_id),
            "run_status": run.status,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "cancelled"
    assert [record.event for record in result["events"]] == [EVENT_RUN_STARTED, EVENT_RUN_CANCELLED]
    assert result["events"][1].data["status"] == "cancelled"
    assert result["run_status"] == "cancelled"
