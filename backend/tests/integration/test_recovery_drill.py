"""P1 恢复演练：杀 worker 收敛、重复投递恰好一次、重启补发、Redis 抖动降级与全链路验收。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
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
from app.agents.middlewares.credential_guard import scan_sensitive_plaintext
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.db import get_session
from app.main import app
from app.models.audit import ModelAudit, ToolAudit
from app.models.runtime import AgentRunAttempt, Message, User
from app.repositories import (
    LEASE_EXPIRED_ERROR,
    AgentRunRepository,
    MessageRepository,
    RequestRepository,
    UserRepository,
)
from app.routers.chat_router import (
    DEFAULT_DEV_USERNAME,
    get_run_dispatcher,
    get_run_stream_service,
    get_stream_guest,
)
from app.services.run_body import AgentRunBody
from app.services.run_events import (
    EVENT_MESSAGE_COMPLETED,
    EVENT_MESSAGE_DELTA,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_STARTED,
)
from app.services.run_stream import RunStreamService
from app.services.run_worker import (
    RunContext,
    RunExecutor,
    reconcile_expired_runs,
    repost_pending_runs,
)
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
DRILL_USER = "drill-guest"


@dataclass
class BlockingBody:
    """进入后挂起：模拟杀 worker 时仍在执行的进程内任务。"""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, ctx: RunContext) -> None:
        self.entered.set()
        await self.release.wait()


class FlakyRunEventBus:
    """Redis 抖动替身：所有读写都抛错，模拟运行期 Redis 不可用。"""

    async def publish(self, run_id: UUID, event: str, data: Any) -> None:
        raise ConnectionError("redis flutter")

    async def read(self, run_id: UUID, *, after: str | None = None, count: int = 256) -> list[Any]:
        raise ConnectionError("redis flutter")

    async def follow(
        self,
        run_id: UUID,
        *,
        after: str | None = None,
        count: int = 64,
        block_ms: int = 2000,
    ) -> list[Any]:
        raise ConnectionError("redis flutter")

    async def aclose(self) -> None:
        return None


async def _count(factory: SessionFactoryArg, stmt: Any) -> int:
    async with factory() as session:
        return int((await session.execute(stmt)).scalar_one())


def _assistant_count_stmt(thread_id: UUID) -> Any:
    return (
        select(func.count())
        .select_from(Message)
        .where(Message.thread_id == thread_id, Message.role == "assistant")
    )


def _run_audit_count_stmt(run_id: UUID, model: Any) -> Any:
    return select(func.count()).select_from(model).where(model.run_id == str(run_id))


def test_drill_kill_worker_converges_and_projects_terminal(runtime_db_url: str) -> None:
    """杀 worker：租约过期由 reconciler 收敛为 failed，接力队头，投影从数据库补齐。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        dispatcher = RecordingDispatcher()
        async with factory() as session:
            first = await intake_message(session, username=DRILL_USER, text="长任务")
            queued = await intake_message(
                session, username=DRILL_USER, text="排队中", thread_id=first.thread_id
            )
        assert first.run_id is not None
        assert queued.run_id is None

        body = BlockingBody()
        executor = RunExecutor(
            body,
            dispatcher=dispatcher,
            session_factory=factory,
            event_bus=bus,
            lease_seconds=1,
            heartbeat_interval_seconds=30,
        )
        task = asyncio.create_task(executor.execute(first.run_id))
        await body.entered.wait()

        # 杀 worker：进程消失，终态与事件都来不及写
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(1.2)  # 等租约过期
        converged = await reconcile_expired_runs(dispatcher, session_factory=factory)

        async with factory() as session:
            run = await AgentRunRepository(session).get_by_id(first.run_id)
            attempt = (
                await session.execute(
                    select(AgentRunAttempt).where(AgentRunAttempt.run_id == first.run_id)
                )
            ).scalar_one()
            request = await RequestRepository(session).get_by_id(first.request_id)
            relayed = await AgentRunRepository(session).get_by_request_id(queued.request_id)
        assert run is not None and request is not None and relayed is not None

        service = RunStreamService(bus=bus, session_factory=factory, follow_block_ms=10)
        view = await service.authorize(run_id=first.run_id, user_id=run.user_id)
        chunks = [chunk async for chunk in service.stream(run=view)]
        return {
            "converged": converged,
            "run_status": run.status,
            "run_error": run.error,
            "attempt_outcome": attempt.outcome,
            "attempt_error_class": attempt.error_class,
            "attempt_finished": attempt.finished_at is not None,
            "request_status": request.status,
            "assistant_count": await _count(factory, _assistant_count_stmt(first.thread_id)),
            "model_count": await _count(factory, _run_audit_count_stmt(first.run_id, ModelAudit)),
            "events": [record.event for record in bus.records(first.run_id)],
            "enqueued": dispatcher.enqueued,
            "relayed_run_id": relayed.id,
            "relayed_status": relayed.status,
            "chunks": chunks,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["converged"] == 1
    assert result["run_status"] == "failed"
    assert result["run_error"] == LEASE_EXPIRED_ERROR
    assert result["attempt_outcome"] == "lease_expired"
    assert result["attempt_error_class"] == LEASE_EXPIRED_ERROR
    assert result["attempt_finished"] is True
    assert result["request_status"] == "finished"
    assert result["assistant_count"] == 0
    assert result["model_count"] == 0
    # 被杀 worker 只留下 run.started：不伪装完成，也不写终态
    assert result["events"] == [EVENT_RUN_STARTED]
    # 接力：队头请求被派发，线程不因 worker 死亡而卡死
    assert result["enqueued"] == [result["relayed_run_id"]]
    assert result["relayed_status"] == "pending"
    # 事件缺失由数据库投影补齐（failed 终态 + worker_lease_expired 原因）
    assert [chunk.splitlines()[1] for chunk in result["chunks"]] == [
        "event: run.started",
        "event: run.failed",
    ]
    assert result["chunks"][1].startswith("id: projected-1\nevent: run.failed")
    assert '"error": "worker_lease_expired"' in result["chunks"][1]
    assert '"projected": true' in result["chunks"][1]


def test_drill_duplicate_delivery_executes_once(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重复投递（顺序 + 并发）：认领是条件更新，恰好一个执行体持租约，副作用只发生一次。"""
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        bus = RecordingRunEventBus()

        def make_executor() -> RunExecutor:
            return RunExecutor(
                AgentRunBody(checkpointer=InMemorySaver()),
                dispatcher=RecordingDispatcher(),
                session_factory=factory,
                event_bus=bus,
            )

        async with factory() as session:
            first = await intake_message(session, username=DRILL_USER, text="重复投递")
        assert first.run_id is not None
        executor = make_executor()
        sequential = [await executor.execute(first.run_id), await executor.execute(first.run_id)]

        async with factory() as session:
            second = await intake_message(session, username="drill-race-guest", text="并发投递")
        assert second.run_id is not None
        concurrent = list(
            await asyncio.gather(
                make_executor().execute(second.run_id),
                make_executor().execute(second.run_id),
            )
        )

        return {
            "sequential": sequential,
            "concurrent": sorted(concurrent),
            "first_assistant": await _count(factory, _assistant_count_stmt(first.thread_id)),
            "first_models": await _count(factory, _run_audit_count_stmt(first.run_id, ModelAudit)),
            "first_attempts": await _count(
                factory,
                select(func.count())
                .select_from(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == first.run_id),
            ),
            "second_assistant": await _count(factory, _assistant_count_stmt(second.thread_id)),
            "second_attempts": await _count(
                factory,
                select(func.count())
                .select_from(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == second.run_id),
            ),
            "second_events": [record.event for record in bus.records(second.run_id)],
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["sequential"] == ["completed", "not_claimable"]
    assert result["concurrent"] == ["completed", "not_claimable"]
    assert result["first_assistant"] == 1
    assert result["first_models"] == 1
    assert result["first_attempts"] == 1
    assert result["second_assistant"] == 1
    assert result["second_attempts"] == 1
    # 只有一个执行体发过事件链：重复投递不产生第二组投影
    assert result["second_events"] == [
        EVENT_RUN_STARTED,
        EVENT_MESSAGE_DELTA,
        EVENT_MESSAGE_COMPLETED,
        EVENT_RUN_COMPLETED,
    ]


def test_drill_restart_reposts_only_pending(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 重启：补发只覆盖 pending；重复投递由 claim 兜底，补发后恰好执行一次。"""
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            done = await intake_message(session, username=DRILL_USER, text="重启前已完成")
        assert done.run_id is not None
        worker = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        assert await worker.execute(done.run_id) == "completed"

        async with factory() as session:
            live = await intake_message(session, username="drill-live-guest", text="他机运行中")
            lost_a = await intake_message(session, username="drill-lost-a", text="投递丢失 A")
            lost_b = await intake_message(session, username="drill-lost-b", text="投递丢失 B")
            assert live.run_id is not None
            assert lost_a.run_id is not None and lost_b.run_id is not None
            # 他机存活 worker 仍持有租约：重启恢复不得抢走
            await AgentRunRepository(session).claim(
                live.run_id, owner_token="live-worker", lease_seconds=60
            )
            await session.commit()
            lost_run_ids = {lost_a.run_id, lost_b.run_id}
            lost_threads = (lost_a.thread_id, lost_b.thread_id)

        dispatcher = RecordingDispatcher()
        reposted = await repost_pending_runs(dispatcher, session_factory=factory)
        reposted_again = await repost_pending_runs(dispatcher, session_factory=factory)

        # 只执行本测试自己的 Run：补发是全库扫描，会话级共享库会带入其他用例的 pending
        mine = (lost_a.run_id, lost_b.run_id)
        outcomes = [await worker.execute(run_id) for run_id in mine]
        outcomes_again = [await worker.execute(run_id) for run_id in mine]

        async with factory() as session:
            live_run = await AgentRunRepository(session).get_by_id(live.run_id)
            done_run = await AgentRunRepository(session).get_by_id(done.run_id)
        assert live_run is not None and done_run is not None
        return {
            "reposted": reposted,
            "reposted_again": reposted_again,
            "lost_enqueued": lost_run_ids <= set(dispatcher.enqueued),
            "live_enqueued": live.run_id in dispatcher.enqueued,
            "done_enqueued": done.run_id in dispatcher.enqueued,
            "outcomes": outcomes,
            "outcomes_again": outcomes_again,
            "live_status": live_run.status,
            "done_status": done_run.status,
            "replies": {
                str(thread_id): await _count(factory, _assistant_count_stmt(thread_id))
                for thread_id in lost_threads
            },
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["reposted"] >= 2
    assert result["lost_enqueued"] is True
    assert result["live_enqueued"] is False
    assert result["done_enqueued"] is False
    # pending 意图在补发后依旧成立：重复补发幂等，重复执行由 claim 拦下
    assert result["reposted_again"] >= 2
    assert result["outcomes"] == ["completed", "completed"]
    assert result["outcomes_again"] == ["not_claimable", "not_claimable"]
    assert result["live_status"] == "running"
    assert result["done_status"] == "completed"
    assert set(result["replies"].values()) == {1}


def test_drill_redis_flutter_keeps_facts_and_stream_fallback(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis 抖动：事件发布失败不断运行，事实照常落库；SSE 退回数据库状态投影。"""
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=FlakyRunEventBus(),
        )
        async with factory() as session:
            intake = await intake_message(session, username=DRILL_USER, text="Redis 抖动")
            user = await UserRepository(session).get_or_create(username=DRILL_USER)
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            run = await AgentRunRepository(session).get_by_id(intake.run_id)
            messages = await MessageRepository(session).list_for_thread(intake.thread_id)
        assert run is not None
        service = RunStreamService(
            bus=FlakyRunEventBus(), session_factory=factory, follow_block_ms=10
        )
        view = await service.authorize(run_id=intake.run_id, user_id=user.id)
        chunks = [chunk async for chunk in service.stream(run=view)]
        return {
            "outcome": outcome,
            "run_status": run.status,
            "reply": next(
                (
                    message.content.get("text")
                    for message in messages
                    if message.role == "assistant"
                ),
                None,
            ),
            "model_count": await _count(factory, _run_audit_count_stmt(intake.run_id, ModelAudit)),
            "chunks": chunks,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["run_status"] == "completed"
    assert result["model_count"] == 1
    assert str(result["reply"]).startswith("[dev-stub] 已收到：")
    assert [chunk.splitlines()[1] for chunk in result["chunks"]] == [
        "event: message.completed",
        "event: run.completed",
    ]
    assert '"projected": true' in result["chunks"][0]
    assert '"text": "[dev-stub] 已收到：Redis 抖动"' in result["chunks"][0]


def test_drill_plaintext_scan_skeleton(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明文扫描骨架：扫描器可命中三类敏感明文；真实 Run 的消息、审计与事件投影零命中。"""
    force_stub_model(monkeypatch)
    hits = scan_sensitive_plaintext("门锁密码：839201，身份证 110101199001011234，电话 13800138000")
    assert {hit.category for hit in hits} == {"password", "id_card", "phone"}

    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def test_echo(text: str) -> str:
        """回显文本。"""
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
            intake = await intake_message(session, username=DRILL_USER, text="现在几点")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        artifacts: list[tuple[str, str]] = []
        async with factory() as session:
            for message in await MessageRepository(session).list_for_thread(intake.thread_id):
                artifacts.append(
                    (f"message:{message.role}", json.dumps(message.content, ensure_ascii=False))
                )
            for row in (
                (
                    await session.execute(
                        select(ModelAudit).where(ModelAudit.run_id == str(intake.run_id))
                    )
                )
                .scalars()
                .all()
            ):
                artifacts.append(
                    (
                        "model_audit",
                        json.dumps({"model": row.model, "error": row.error}, ensure_ascii=False),
                    )
                )
            for row in (
                (
                    await session.execute(
                        select(ToolAudit).where(ToolAudit.run_id == str(intake.run_id))
                    )
                )
                .scalars()
                .all()
            ):
                artifacts.append(
                    (
                        "tool_audit",
                        json.dumps(
                            {"args": row.args_digest, "error": row.error}, ensure_ascii=False
                        ),
                    )
                )
        for record in bus.records(intake.run_id):
            artifacts.append((f"event:{record.event}", json.dumps(record.data, ensure_ascii=False)))
        return {"outcome": outcome, "artifacts": artifacts}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert len(result["artifacts"]) >= 6
    for source, text in result["artifacts"]:
        assert scan_sensitive_plaintext(text) == [], f"敏感明文进入产物：{source}"


def test_drill_full_chain_message_to_stream_and_audit(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1 退出标准：消息 → Run → 模型 → SSE 全链路可达，审计行完整可查。"""
    force_stub_model(monkeypatch)
    engine: AsyncEngine = create_async_engine(runtime_db_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    bus = RecordingRunEventBus()
    dispatcher = RecordingDispatcher()
    username = f"drill-e2e-{uuid4().hex[:8]}"

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    async def _override_guest(
        x_dev_username: Annotated[str, Header(alias="X-Dev-Username")] = DEFAULT_DEV_USERNAME,
    ) -> User:
        async with factory() as session:
            user = await UserRepository(session).get_or_create(username=x_dev_username)
            await session.commit()
            return user

    def _override_service() -> RunStreamService:
        return RunStreamService(bus=bus, session_factory=factory, follow_block_ms=10)

    async def _execute(run_id: str) -> str:
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        return await executor.execute(UUID(run_id))

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_run_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_stream_guest] = _override_guest
    app.dependency_overrides[get_run_stream_service] = _override_service
    try:
        with TestClient(app) as client:
            headers = {"X-Dev-Username": username}
            submitted = client.post(
                "/api/guest/chat/messages",
                json={"content": {"text": "有哪些房型"}},
                headers=headers,
            )
            assert submitted.status_code == 201
            payload = submitted.json()
            outcome = asyncio.run(_execute(payload["run_id"]))

            streaming = client.get(
                f"/api/guest/chat/stream?run_id={payload['run_id']}", headers=headers
            )
            audits = client.get(
                f"/api/guest/chat/threads/{payload['thread_id']}/audits", headers=headers
            )
            status = client.get(
                f"/api/guest/chat/requests/{payload['request_id']}", headers=headers
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())

    assert outcome == "completed"
    assert dispatcher.enqueued == [UUID(payload["run_id"])]
    assert streaming.status_code == 200
    assert "event: message.delta" in streaming.text
    assert "event: message.completed" in streaming.text
    assert "event: run.completed" in streaming.text
    assert status.json()["run_status"] == "completed"
    timeline = audits.json()
    assert timeline["truncated"] is False
    entry = next(item for item in timeline["entries"] if item["kind"] == "model")
    assert entry["run_id"] == payload["run_id"]
    assert entry["status"] == "completed"
    assert entry["prompt_tokens"] is not None and entry["prompt_tokens"] > 0
    assert entry["cached_prompt_tokens"] == 0
