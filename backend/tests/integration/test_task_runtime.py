"""后台任务运行时集成测试（真实 PostgreSQL）：去重、抢占、心跳、终态守卫与收敛。

覆盖机制基线 §2.3：重复投递不能取得运行中任务、claim 上限 4、失联统一失败
（不自动重放）、未知 handler_version fail-closed、失败钩子与终态同事务。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.task import Task
from app.repositories.task_repository import UNKNOWN_HANDLER_ERROR, TaskRepository
from app.services.task_registry import TaskContext, reset_registry, task_handler
from app.services.task_service import (
    TaskExecutor,
    cancel_task,
    reconcile_expired_tasks,
    repost_pending_tasks,
    submit_task,
)
from tests.integration.db_utils import run_with_session, run_with_session_factory

pytestmark = pytest.mark.integration


class RecordingDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_task(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def aclose(self) -> None:
        return None


class FailingDispatcher:
    async def enqueue_task(self, task_id: UUID) -> None:
        raise RuntimeError("redis 不可达")

    async def aclose(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    reset_registry()
    yield
    reset_registry()


def _unique_task_type(prefix: str = "test.task") -> str:
    return f"{prefix}.{uuid4().hex[:8]}"


async def _load_task(factory: async_sessionmaker[AsyncSession], task_id: UUID) -> Task:
    async with factory() as session:
        task = await TaskRepository(session).get_by_id(task_id)
        assert task is not None
        return task


def test_submit_creates_pending_and_deduplicates(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        dispatcher = RecordingDispatcher()
        first = await submit_task(
            task_type="test.dedupe",
            handler_version=1,
            payload={"value": 1},
            dedupe_key="demo:1",
            dispatcher=dispatcher,
            session_factory=factory,
        )
        second = await submit_task(
            task_type="test.dedupe",
            handler_version=1,
            payload={"value": 1},
            dedupe_key="demo:1",
            dispatcher=dispatcher,
            session_factory=factory,
        )
        third = await submit_task(
            task_type="test.dedupe",
            handler_version=1,
            payload={"value": 2},
            dedupe_key=None,
            dispatcher=dispatcher,
            session_factory=factory,
        )
        task = await _load_task(factory, first.task_id)
        return {
            "same_id": first.task_id == second.task_id,
            "second_dedup": second.deduplicated,
            "first_dedup": first.deduplicated,
            "third_dedup": third.deduplicated,
            "status": task.status,
            "payload": task.payload,
            "enqueued": dispatcher.enqueued,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["same_id"] is True
    assert values["second_dedup"] is True
    assert values["first_dedup"] is False
    assert values["third_dedup"] is False
    assert values["status"] == "pending"
    assert values["payload"] == {"value": 1}
    # 只投递新建任务：去重命中不再投递，无去重键的第二个任务单独投递
    assert len(values["enqueued"]) == 2


def test_submit_survives_dispatch_failure(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> str:
        submission = await submit_task(
            task_type="test.dispatch",
            handler_version=1,
            payload={},
            dispatcher=FailingDispatcher(),
            session_factory=factory,
        )
        task = await _load_task(factory, submission.task_id)
        return task.status

    assert run_with_session_factory(runtime_db_url, _body) == "pending"


def test_execute_success_path(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.success")
    seen: list[dict[str, Any]] = []

    @task_handler(task_type, 1)
    async def _body(ctx: TaskContext) -> dict[str, object]:
        seen.append({"attempt": ctx.attempt_no, "payload": dict(ctx.payload)})
        return {"handled": True}

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type,
            handler_version=1,
            payload={"ticket": "A-1"},
            session_factory=factory,
        )
        executor = TaskExecutor(session_factory=factory)
        outcome = await executor.execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {
            "outcome": outcome,
            "status": task.status,
            "result": task.result,
            "attempt_count": task.attempt_count,
            "owner_token": task.owner_token,
            "lease": task.lease_expires_at,
            "finished_at": task.finished_at,
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    assert values["status"] == "succeeded"
    assert values["result"] == {"handled": True}
    assert values["attempt_count"] == 1
    assert values["owner_token"] is None
    assert values["lease"] is None
    assert values["finished_at"] is not None
    assert seen == [{"attempt": 1, "payload": {"ticket": "A-1"}}]


def test_duplicate_delivery_cannot_claim_running_task(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.duplicate")
    body_runs = 0

    @task_handler(task_type, 1)
    async def _body(ctx: TaskContext) -> dict[str, object]:
        nonlocal body_runs
        body_runs += 1
        await asyncio.sleep(0.3)
        return {"runs": body_runs}

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=1, payload={}, session_factory=factory
        )
        executor = TaskExecutor(session_factory=factory)

        async def _deliver() -> str:
            return await executor.execute(submission.task_id)

        outcomes = await asyncio.gather(_deliver(), _deliver())
        task = await _load_task(factory, submission.task_id)
        return {
            "outcomes": sorted(outcomes),
            "body_runs": body_runs,
            "status": task.status,
            "attempt_count": task.attempt_count,
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcomes"] == ["not_claimable", "succeeded"]
    assert values["body_runs"] == 1
    assert values["attempt_count"] == 1
    assert values["status"] == "succeeded"


def test_unknown_handler_version_fails_closed(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.unknown")

    @task_handler(task_type, 1)
    async def _v1(ctx: TaskContext) -> None:
        return None

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=3, payload={}, session_factory=factory
        )
        executor = TaskExecutor(session_factory=factory)
        first = await executor.execute(submission.task_id)
        second = await executor.execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {"first": first, "second": second, "status": task.status, "error": task.error}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["first"] == "failed"
    assert values["second"] == "not_claimable"
    assert values["status"] == "failed"
    assert values["error"] == UNKNOWN_HANDLER_ERROR


def test_handler_failure_runs_hook_in_terminal_transaction(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.hook")
    hook_state: list[dict[str, Any]] = []

    async def _on_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
        # 与终态写入同事务：此时应已能看到 failed 状态（提交前可见）
        row = (
            await session.execute(select(Task.status).where(Task.id == ctx.task_id))
        ).scalar_one()
        hook_state.append({"status_in_tx": row, "error": error, "task_id": ctx.task_id})

    @task_handler(task_type, 1, on_failure=_on_failure)
    async def _body(ctx: TaskContext) -> None:
        raise RuntimeError("解析器不可用")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=1, payload={}, session_factory=factory
        )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {"outcome": outcome, "status": task.status, "error": task.error}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "failed"
    assert values["status"] == "failed"
    assert values["error"] == "解析器不可用"
    assert len(hook_state) == 1
    assert hook_state[0]["status_in_tx"] == "failed"


def test_failure_hook_error_does_not_mask_terminal_state(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.hook-error")

    async def _on_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
        raise RuntimeError("补偿动作失败")

    @task_handler(task_type, 1, on_failure=_on_failure)
    async def _body(ctx: TaskContext) -> None:
        raise ValueError("业务失败")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=1, payload={}, session_factory=factory
        )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {"outcome": outcome, "status": task.status, "error": task.error}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "failed"
    assert values["status"] == "failed"
    assert values["error"] == "业务失败"


def test_heartbeat_keeps_lease_alive_for_slow_handler(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.heartbeat")

    @task_handler(task_type, 1)
    async def _slow(ctx: TaskContext) -> dict[str, object]:
        await asyncio.sleep(0.6)
        return {"ok": True}

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=1, payload={}, session_factory=factory
        )
        executor = TaskExecutor(
            session_factory=factory, lease_seconds=1, heartbeat_interval_seconds=0.2
        )
        outcome = await executor.execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {"outcome": outcome, "status": task.status}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values == {"outcome": "succeeded", "status": "succeeded"}


def test_expired_lease_converges_to_failed_with_hook(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.expired")
    hooks: list[str] = []

    async def _on_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
        hooks.append(error)

    @task_handler(task_type, 1, on_failure=_on_failure)
    async def _body(ctx: TaskContext) -> None:
        return None

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            task = await tasks.create(task_type=task_type, handler_version=1, payload={})
            claimed = await tasks.claim(task.id, owner_token="dead-worker", lease_seconds=-60)
            await session.commit()
            assert claimed == 1
            task_id = task.id

        converged = await reconcile_expired_tasks(session_factory=factory)
        task = await _load_task(factory, task_id)
        return {
            "converged": converged,
            "status": task.status,
            "error": task.error,
            "owner_token": task.owner_token,
            "hooks": list(hooks),
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["converged"] >= 1
    assert values["status"] == "failed"
    assert values["error"] == "worker_lease_expired"
    assert values["owner_token"] is None
    assert values["hooks"] == ["worker_lease_expired"]


def test_live_lease_is_not_converged(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.live")

    @task_handler(task_type, 1)
    async def _body(ctx: TaskContext) -> None:
        return None

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            task = await tasks.create(task_type=task_type, handler_version=1, payload={})
            claimed = await tasks.claim(task.id, owner_token="alive", lease_seconds=600)
            await session.commit()
            assert claimed == 1
            task_id = task.id

        converged = await reconcile_expired_tasks(session_factory=factory)
        current = await _load_task(factory, task_id)
        return {"converged": converged, "status": current.status, "owner": current.owner_token}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["status"] == "running"
    assert values["owner"] == "alive"


def test_claim_limit_exceeded_fails_closed(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.limit")

    @task_handler(task_type, 1)
    async def _body(ctx: TaskContext) -> None:
        return None

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            task = await TaskRepository(session).create(
                task_type=task_type, handler_version=1, payload={}
            )
            task.attempt_count = 4
            await session.commit()
            task_id = task.id

        outcome = await TaskExecutor(session_factory=factory).execute(task_id)
        current = await _load_task(factory, task_id)
        return {"outcome": outcome, "status": current.status, "error": current.error}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values == {
        "outcome": "claim_limit_exceeded",
        "status": "failed",
        "error": "claim_limit_exceeded",
    }


def test_reconcile_fails_claim_exhausted_pending_tasks(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.exhausted")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            task = await TaskRepository(session).create(
                task_type=task_type, handler_version=1, payload={}
            )
            task.attempt_count = 4
            await session.commit()
            task_id = task.id

        converged = await reconcile_expired_tasks(session_factory=factory)
        current = await _load_task(factory, task_id)
        return {"converged": converged, "status": current.status, "error": current.error}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["converged"] >= 1
    assert values["status"] == "failed"
    assert values["error"] == "claim_limit_exceeded"


def test_terminal_write_requires_current_owner(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.owner")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            task = await tasks.create(task_type=task_type, handler_version=1, payload={})
            assert await tasks.claim(task.id, owner_token="w1", lease_seconds=60) == 1
            await session.commit()
            task_id = task.id

        async with factory() as session:
            tasks = TaskRepository(session)
            stale_success = await tasks.mark_succeeded(
                task_id, owner_token="w0", result={"stale": True}
            )
            stale_failure = await tasks.mark_failed(task_id, owner_token="w0", error="stale")
            current_success = await tasks.mark_succeeded(
                task_id, owner_token="w1", result={"ok": True}
            )
            await session.commit()

        async with factory() as session:
            tasks = TaskRepository(session)
            second_success = await tasks.mark_succeeded(
                task_id, owner_token="w1", result={"again": True}
            )
            await session.commit()

        current = await _load_task(factory, task_id)
        return {
            "stale_success": stale_success,
            "stale_failure": stale_failure,
            "current_success": current_success,
            "second_success": second_success,
            "status": current.status,
            "result": current.result,
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["stale_success"] is False
    assert values["stale_failure"] is False
    assert values["current_success"] is True
    assert values["second_success"] is False
    assert values["status"] == "succeeded"
    assert values["result"] == {"ok": True}


def test_heartbeat_rejected_after_terminal(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.hb-terminal")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            task = await tasks.create(task_type=task_type, handler_version=1, payload={})
            assert await tasks.claim(task.id, owner_token="w1", lease_seconds=60) == 1
            assert await tasks.heartbeat(task.id, owner_token="w1", lease_seconds=60) is True
            assert await tasks.mark_succeeded(task.id, owner_token="w1") is True
            await session.commit()
            task_id = task.id

        async with factory() as session:
            alive = await TaskRepository(session).heartbeat(
                task_id, owner_token="w1", lease_seconds=60
            )
            await session.commit()
        return {"alive": alive}

    assert run_with_session_factory(runtime_db_url, _run) == {"alive": False}


def test_cancel_pending_task_then_execute_is_not_claimable(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.cancel")
    runs: list[int] = []

    @task_handler(task_type, 1)
    async def _body(ctx: TaskContext) -> None:
        runs.append(1)

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        submission = await submit_task(
            task_type=task_type, handler_version=1, payload={}, session_factory=factory
        )
        cancelled = await cancel_task(submission.task_id, session_factory=factory)
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)
        task = await _load_task(factory, submission.task_id)
        return {"cancelled": cancelled, "outcome": outcome, "status": task.status, "runs": runs}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values == {
        "cancelled": True,
        "outcome": "not_claimable",
        "status": "cancelled",
        "runs": [],
    }


def test_repost_pending_tasks_with_dispatcher(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.repost")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            first = await tasks.create(task_type=task_type, handler_version=1, payload={})
            second = await tasks.create(task_type=task_type, handler_version=1, payload={})
            third = await tasks.create(task_type=task_type, handler_version=1, payload={})
            assert await tasks.claim(third.id, owner_token="w", lease_seconds=600) == 1
            await session.commit()
            pending_ids = {task.id for task in await tasks.list_pending(limit=500)}

        dispatcher = RecordingDispatcher()
        await repost_pending_tasks(dispatcher, session_factory=factory)
        return {
            "own_pending_included": {first.id, second.id} <= set(dispatcher.enqueued),
            "running_excluded": third.id not in dispatcher.enqueued,
            "pending_ids_included": pending_ids <= set(dispatcher.enqueued),
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values == {
        "own_pending_included": True,
        "running_excluded": True,
        "pending_ids_included": True,
    }


def test_progress_write_requires_owner(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.progress")

    async def _run(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            tasks = TaskRepository(session)
            task = await tasks.create(task_type=task_type, handler_version=1, payload={})
            assert await tasks.claim(task.id, owner_token="w1", lease_seconds=60) == 1
            stale = await tasks.set_progress(task.id, owner_token="w0", progress={"pct": 10})
            fresh = await tasks.set_progress(task.id, owner_token="w1", progress={"pct": 90})
            await session.commit()
            task_id = task.id

        current = await _load_task(factory, task_id)
        return {"stale": stale, "fresh": fresh, "progress": current.progress}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["stale"] is False
    assert values["fresh"] is True
    assert values["progress"] == {"pct": 90}


def test_claim_increments_attempt_and_sets_lease(runtime_db_url: str) -> None:
    task_type = _unique_task_type("test.claim")

    async def _body(session: AsyncSession) -> dict[str, Any]:
        tasks = TaskRepository(session)
        task = await tasks.create(task_type=task_type, handler_version=1, payload={})
        assert await tasks.claim(task.id, owner_token="w1", lease_seconds=120) == 1
        await session.commit()

        row = (
            await session.execute(
                select(
                    Task.status,
                    Task.attempt_count,
                    Task.owner_token,
                    Task.last_heartbeat_at,
                    Task.lease_expires_at,
                    (Task.lease_expires_at - Task.last_heartbeat_at).label("lease_span"),
                ).where(Task.id == task.id)
            )
        ).one()
        return {
            "status": row.status,
            "attempt_count": row.attempt_count,
            "owner": row.owner_token,
            "lease_span": row.lease_span.total_seconds(),
            "heartbeat_set": row.last_heartbeat_at is not None,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "running"
    assert values["attempt_count"] == 1
    assert values["owner"] == "w1"
    assert 118 <= values["lease_span"] <= 122
    assert values["heartbeat_set"] is True
