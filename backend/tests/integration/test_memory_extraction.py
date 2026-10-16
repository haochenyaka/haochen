"""P1.4 记忆写入链路集成测试（真实 PostgreSQL）：终态触发任务 + 抽取落 GuestMemory。

覆盖：Run 以 completed 收敛时提交 `memory.extract` 任务并投递；处理器校验候选后写
`guest_memories`（批内与既有去重、敏感丢弃、来源标记）；failed Run 不触发任务。
抽取器以替身注入，不依赖外部模型。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.memory.extraction import MemoryCandidate
from app.memory.service import MEMORY_EXTRACT_TASK_TYPE, MEMORY_SOURCE
from app.memory.task_handlers import extract_memories
from app.models.domain import GuestMemory
from app.models.task import Task
from app.repositories import GuestRepository, UserRepository
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from app.services.task_registry import TaskContext
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class RecordingTaskDispatcher:
    """记录投递的 task id：Run 终态后记忆任务的投递口替身。"""

    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_task(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def aclose(self) -> None:
        return None


class FakeExtractor:
    def __init__(self, candidates: list[MemoryCandidate]) -> None:
        self._candidates = candidates

    async def extract(self, *, transcript: str) -> list[MemoryCandidate]:
        return self._candidates


async def _memory_tasks(factory: SessionFactoryArg, run_id: UUID) -> list[Task]:
    async with factory() as session:
        rows = await session.execute(select(Task).where(Task.task_type == MEMORY_EXTRACT_TASK_TYPE))
    return [task for task in rows.scalars().all() if task.payload.get("run_id") == str(run_id)]


def test_completed_run_submits_and_dispatches_memory_task(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    monkeypatch.setattr(get_settings(), "memory_extract_enabled", True)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        username = f"mem-{uuid4().hex[:8]}"
        async with factory() as session:
            intake = await intake_message(session, username=username, text="你好")
        assert intake.run_id is not None
        task_dispatcher = RecordingTaskDispatcher()
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            task_dispatcher=task_dispatcher,
            session_factory=factory,
        )
        outcome = await executor.execute(intake.run_id)
        tasks = await _memory_tasks(factory, intake.run_id)
        return {
            "outcome": outcome,
            "task_count": len(tasks),
            "task_status": tasks[0].status if tasks else None,
            "dedupe_key": tasks[0].dedupe_key if tasks else None,
            "enqueued": [task.id for task in tasks],
            "dispatched": list(task_dispatcher.enqueued),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["task_count"] == 1
    assert result["task_status"] == "pending"
    assert result["dedupe_key"].startswith("memory.extract:")
    # 新建的 pending 任务会被投递（投递 id 即落库任务 id）
    assert result["dispatched"] == result["enqueued"]


def test_handler_persists_candidates_deduplicated(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    monkeypatch.setattr(get_settings(), "memory_extract_enabled", True)
    duplicated = [
        MemoryCandidate("preference", "偏好高层安静房间"),
        MemoryCandidate("preference", "偏好高层安静房间"),
    ]
    monkeypatch.setattr(
        "app.memory.task_handlers.build_memory_extractor", lambda: FakeExtractor(duplicated)
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        username = f"mem-{uuid4().hex[:8]}"
        async with factory() as session:
            intake = await intake_message(session, username=username, text="我喜欢高层安静的房间")
            user = await UserRepository(session).get_or_create(username=username)
            guest = await GuestRepository(session).get_or_create_by_user(user.id)
            await session.commit()
        assert intake.run_id is not None
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            task_dispatcher=RecordingTaskDispatcher(),
            session_factory=factory,
        )
        outcome = await executor.execute(intake.run_id)

        ctx = TaskContext(
            task_id=uuid4(),
            task_type=MEMORY_EXTRACT_TASK_TYPE,
            handler_version=1,
            payload={
                "run_id": str(intake.run_id),
                "thread_id": str(intake.thread_id),
                "user_id": str(user.id),
                "agent_slug": "front-desk",
            },
            attempt_no=1,
            owner_token="memory-test",
            session_factory=factory,
        )
        first = await extract_memories(ctx)
        second = await extract_memories(ctx)
        async with factory() as session:
            rows = (
                (await session.execute(select(GuestMemory).where(GuestMemory.guest_id == guest.id)))
                .scalars()
                .all()
            )
        return {
            "outcome": outcome,
            "first": dict(first),
            "second": dict(second),
            "contents": [row.content for row in rows],
            "sources": [row.source for row in rows],
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["first"]["stored"] == 1  # 批内重复只落一条
    assert result["second"]["stored"] == 0  # 重跑与既有记忆去重，幂等
    assert result["contents"] == ["偏好高层安静房间"]
    assert result["sources"] == [MEMORY_SOURCE]


def test_handler_skips_when_run_not_completed(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    monkeypatch.setattr(get_settings(), "memory_extract_enabled", True)
    monkeypatch.setattr(
        "app.memory.task_handlers.build_memory_extractor",
        lambda: FakeExtractor([MemoryCandidate("preference", "偏好高层安静房间")]),
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        username = f"mem-{uuid4().hex[:8]}"
        async with factory() as session:
            user = await UserRepository(session).get_or_create(username=username)
            guest = await GuestRepository(session).get_or_create_by_user(user.id)
            await session.commit()
        ctx = TaskContext(
            task_id=uuid4(),
            task_type=MEMORY_EXTRACT_TASK_TYPE,
            handler_version=1,
            payload={
                "run_id": str(uuid4()),  # 不存在的 Run
                "thread_id": str(uuid4()),
                "user_id": str(user.id),
                "agent_slug": "front-desk",
            },
            attempt_no=1,
            owner_token="memory-test",
            session_factory=factory,
        )
        result = await extract_memories(ctx)
        async with factory() as session:
            count = (
                (await session.execute(select(GuestMemory).where(GuestMemory.guest_id == guest.id)))
                .scalars()
                .all()
            )
        return {"result": dict(result), "rows": len(count)}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["result"] == {"skipped": "run_not_completed"}
    assert result["rows"] == 0


def test_failed_run_does_not_submit_memory_task(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingModel:
        model_name = "boom-model"

        def bind_tools(self, tools: Any) -> Any:
            return self

        async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
            raise RuntimeError("模型不可用")

    monkeypatch.setattr(get_settings(), "memory_extract_enabled", True)
    monkeypatch.setattr(get_settings(), "model_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: ExplodingModel(),
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        username = f"mem-{uuid4().hex[:8]}"
        async with factory() as session:
            intake = await intake_message(session, username=username, text="你好")
        assert intake.run_id is not None
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            task_dispatcher=RecordingTaskDispatcher(),
            session_factory=factory,
        )
        outcome = await executor.execute(intake.run_id)
        tasks = await _memory_tasks(factory, intake.run_id)
        return {"outcome": outcome, "task_count": len(tasks)}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "failed"
    assert result["task_count"] == 0
