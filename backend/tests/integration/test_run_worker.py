"""Run worker 验证：认领/心跳/终态/接力、取消收敛、过期租约与 pending 补发。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import STUB_MODEL_NAME, STUB_REPLY_PREFIX
from app.models.audit import ModelAudit
from app.models.runtime import AgentRunAttempt
from app.repositories import (
    LEASE_EXPIRED_ERROR,
    AgentRunRepository,
    MessageRepository,
    RequestRepository,
)
from app.services.audit_service import MODEL_STATUS_COMPLETED
from app.services.run_worker import (
    RunCancelled,
    RunContext,
    RunExecutor,
    RunResult,
    execute_agent_run,
    reconcile_expired_runs,
    repost_pending_runs,
)
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


@dataclass
class RecordingBody:
    calls: list[UUID] = field(default_factory=list)

    async def __call__(self, ctx: RunContext) -> RunResult | None:
        self.calls.append(ctx.identity.run_id)
        return RunResult(token_usage={"prompt_tokens": 3})


@dataclass
class FailingBody:
    async def __call__(self, ctx: RunContext) -> RunResult | None:
        raise RuntimeError("body boom")


@dataclass
class CancelAwareBody:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    saw_cancelled: bool = False

    async def __call__(self, ctx: RunContext) -> RunResult | None:
        self.entered.set()
        await self.proceed.wait()
        try:
            await ctx.ensure_not_cancelled()
        except RunCancelled:
            self.saw_cancelled = True
            raise
        raise AssertionError("取消复核未生效")


@dataclass
class BlockingBody:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, ctx: RunContext) -> RunResult | None:
        self.entered.set()
        await self.release.wait()
        return None


async def _snapshot(
    factory: SessionFactoryArg, *, run_id: UUID, request_id: UUID
) -> dict[str, Any]:
    async with factory() as session:
        run = await AgentRunRepository(session).get_by_id(run_id)
        request = await RequestRepository(session).get_by_id(request_id)
        attempt_count = (
            await session.execute(
                select(func.count())
                .select_from(AgentRunAttempt)
                .where(AgentRunAttempt.run_id == run_id)
            )
        ).scalar_one()
        attempt = (
            await session.execute(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == run_id).limit(1)
            )
        ).scalar_one_or_none()
        assert run is not None and request is not None
        return {
            "run_status": run.status,
            "run_error": run.error,
            "run_error_class": attempt.error_class if attempt else None,
            "attempt_outcome": attempt.outcome if attempt else None,
            "attempt_finished": attempt.finished_at is not None if attempt else None,
            "attempt_count": attempt_count,
            "token_usage": run.token_usage,
            "request_status": request.status,
        }


def test_executor_completes_run_and_relays_head(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            first = await intake_message(session, username="worker-tester")
            second = await intake_message(
                session, username="worker-tester", text="第二条", thread_id=first.thread_id
            )
        assert first.run_id is not None
        assert second.run_id is None

        dispatcher = RecordingDispatcher()
        body = RecordingBody()
        executor = RunExecutor(body, dispatcher=dispatcher, session_factory=factory)

        outcome = await executor.execute(first.run_id)
        snapshot = await _snapshot(factory, run_id=first.run_id, request_id=first.request_id)
        async with factory() as session:
            relayed_run = await AgentRunRepository(session).get_by_request_id(second.request_id)
            second_request = await RequestRepository(session).get_by_id(second.request_id)
            second_position = await RequestRepository(session).queued_position(second.request_id)
        assert second_request is not None
        return {
            "outcome": outcome,
            "run_id": first.run_id,
            "body_calls": body.calls,
            "enqueued": dispatcher.enqueued,
            "relayed_run_id": relayed_run.id if relayed_run else None,
            "relayed_run_status": relayed_run.status if relayed_run else None,
            "second_request_status": second_request.status,
            "second_position": second_position,
            **snapshot,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["run_status"] == "completed"
    assert result["run_error"] is None
    assert result["attempt_outcome"] == "completed"
    assert result["attempt_finished"] is True
    assert result["token_usage"] == {"prompt_tokens": 3}
    assert result["request_status"] == "finished"
    assert result["body_calls"] == [result["run_id"]]
    assert result["enqueued"] == [result["relayed_run_id"]]
    assert result["relayed_run_status"] == "pending"
    assert result["second_request_status"] == "dispatched"
    assert result["second_position"] is None


def test_execute_agent_run_entrypoint_runs_front_desk_graph(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="entrypoint-tester")
        assert intake.run_id is not None

        dispatcher = RecordingDispatcher()
        checkpointer = InMemorySaver()
        outcome = await execute_agent_run(
            {
                "run_dispatcher": dispatcher,
                "session_factory": factory,
                "checkpointer": checkpointer,
            },
            str(intake.run_id),
        )
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        async with factory() as session:
            messages = await MessageRepository(session).list_for_thread(intake.thread_id)
            run_row = await AgentRunRepository(session).get_by_id(intake.run_id)
            audit = (
                await session.execute(
                    select(ModelAudit).where(ModelAudit.run_id == str(intake.run_id))
                )
            ).scalar_one_or_none()
        reply = next((m for m in messages if m.role == "assistant"), None)
        checkpoint_tuple = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": str(intake.thread_id)}}
        )
        assert run_row is not None
        return {
            "outcome": outcome,
            "reply_run_id": reply.run_id if reply else None,
            "reply_text": reply.content.get("text") if reply else None,
            "reply_meta": reply.meta if reply else None,
            "audit_model": audit.model if audit else None,
            "audit_status": audit.status if audit else None,
            "checkpointed": checkpoint_tuple is not None,
            "first_token_at": run_row.first_token_at,
            "started_at": run_row.started_at,
            **snapshot,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["run_status"] == "completed"
    assert result["reply_run_id"] is not None
    assert isinstance(result["reply_text"], str)
    assert result["reply_text"] == f"{STUB_REPLY_PREFIX}你好"
    assert result["reply_meta"] == {"agent_slug": "front-desk"}
    assert result["audit_model"] == STUB_MODEL_NAME
    assert result["audit_status"] == MODEL_STATUS_COMPLETED
    assert result["checkpointed"] is True
    assert result["token_usage"] is not None
    assert result["first_token_at"] is not None  # 首个增量落 TTFT 埋点
    assert result["started_at"] is not None
    assert result["first_token_at"] >= result["started_at"]


def test_executor_returns_not_claimable_for_terminal_run(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="not-claimable-tester")
            assert intake.run_id is not None
            await AgentRunRepository(session).cancel_pending(intake.run_id)
            await session.commit()

        dispatcher = RecordingDispatcher()
        executor = RunExecutor(RecordingBody(), dispatcher=dispatcher, session_factory=factory)
        outcome = await executor.execute(intake.run_id)
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        return {"outcome": outcome, "enqueued": dispatcher.enqueued, **snapshot}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "not_claimable"
    assert result["run_status"] == "cancelled"
    assert result["attempt_count"] == 0
    assert result["enqueued"] == []


def test_executor_body_failure_converges_failed(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="failing-body-tester")
        assert intake.run_id is not None

        dispatcher = RecordingDispatcher()
        executor = RunExecutor(FailingBody(), dispatcher=dispatcher, session_factory=factory)
        outcome = await executor.execute(intake.run_id)
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        return {"outcome": outcome, "enqueued": dispatcher.enqueued, **snapshot}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "failed"
    assert result["run_status"] == "failed"
    assert result["run_error"] == "body boom"
    assert result["run_error_class"] == "RuntimeError"
    assert result["attempt_outcome"] == "failed"
    assert result["request_status"] == "finished"


def test_executor_mid_run_cancel_converges_cancelled(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="cancel-aware-tester")
        assert intake.run_id is not None

        dispatcher = RecordingDispatcher()
        body = CancelAwareBody()
        executor = RunExecutor(body, dispatcher=dispatcher, session_factory=factory)

        task = asyncio.create_task(executor.execute(intake.run_id))
        await body.entered.wait()
        async with factory() as session:
            cancelled = await AgentRunRepository(session).request_cancel(intake.run_id)
            await session.commit()
        assert cancelled is True
        body.proceed.set()
        outcome = await task
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        return {"outcome": outcome, "saw_cancelled": body.saw_cancelled, **snapshot}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["saw_cancelled"] is True
    assert result["outcome"] == "cancelled"
    assert result["run_status"] == "cancelled"
    assert result["attempt_outcome"] == "cancelled"
    assert result["request_status"] == "cancelled"


def test_executor_lease_loss_does_not_overwrite_reconciler(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="lease-lost-tester")
        assert intake.run_id is not None

        dispatcher = RecordingDispatcher()
        body = BlockingBody()
        executor = RunExecutor(
            body,
            dispatcher=dispatcher,
            session_factory=factory,
            lease_seconds=1,
            heartbeat_interval_seconds=30,
        )

        task = asyncio.create_task(executor.execute(intake.run_id))
        await body.entered.wait()
        await asyncio.sleep(1.2)
        converged = await reconcile_expired_runs(dispatcher, session_factory=factory)
        body.release.set()
        outcome = await task
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        return {
            "outcome": outcome,
            "converged": converged,
            "enqueued": dispatcher.enqueued,
            **snapshot,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["converged"] == 1
    assert result["outcome"] == "lease_lost"
    assert result["run_status"] == "failed"
    assert result["run_error"] == LEASE_EXPIRED_ERROR
    assert result["attempt_outcome"] == "lease_expired"
    assert result["request_status"] == "finished"
    assert result["enqueued"] == []


def test_executor_heartbeat_keeps_lease_alive(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="heartbeat-tester")
        assert intake.run_id is not None

        dispatcher = RecordingDispatcher()
        body = BlockingBody()
        executor = RunExecutor(
            body,
            dispatcher=dispatcher,
            session_factory=factory,
            lease_seconds=1,
            heartbeat_interval_seconds=0.2,
        )

        task = asyncio.create_task(executor.execute(intake.run_id))
        await body.entered.wait()
        await asyncio.sleep(1.5)
        converged_mid_run = await reconcile_expired_runs(dispatcher, session_factory=factory)
        body.release.set()
        outcome = await task
        snapshot = await _snapshot(factory, run_id=intake.run_id, request_id=intake.request_id)
        return {"outcome": outcome, "converged_mid_run": converged_mid_run, **snapshot}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["converged_mid_run"] == 0
    assert result["outcome"] == "completed"
    assert result["run_status"] == "completed"


def test_repost_pending_runs_dispatches_only_pending(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            first = await intake_message(session, username="repost-tester")
            second = await intake_message(session, username="repost-tester-2")
            third = await intake_message(session, username="repost-tester-3")
            assert first.run_id and second.run_id and third.run_id
            await AgentRunRepository(session).cancel_pending(third.run_id)
            await session.commit()

        dispatcher = RecordingDispatcher()
        reposted = await repost_pending_runs(dispatcher, session_factory=factory)
        return {
            "reposted": reposted,
            "enqueued": dispatcher.enqueued,
            "expected": {first.run_id, second.run_id},
            "cancelled": third.run_id,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["reposted"] == len(result["enqueued"])
    assert result["expected"] <= set(result["enqueued"])
    assert result["cancelled"] not in result["enqueued"]


def test_reconcile_expired_runs_promotes_next_head(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            first = await intake_message(session, username="reconcile-relay-tester")
            second = await intake_message(
                session, username="reconcile-relay-tester", text="排队中", thread_id=first.thread_id
            )
            assert first.run_id is not None
            assert second.run_id is None
            await AgentRunRepository(session).claim(
                first.run_id, owner_token="dead-worker", lease_seconds=-5
            )
            await session.commit()

        dispatcher = RecordingDispatcher()
        converged = await reconcile_expired_runs(dispatcher, session_factory=factory)
        first_snapshot = await _snapshot(factory, run_id=first.run_id, request_id=first.request_id)
        async with factory() as session:
            relayed_run = await AgentRunRepository(session).get_by_request_id(second.request_id)
            second_request = await RequestRepository(session).get_by_id(second.request_id)
        assert relayed_run is not None and second_request is not None
        return {
            "converged": converged,
            "enqueued": dispatcher.enqueued,
            "relayed_run_id": relayed_run.id,
            "relayed_status": relayed_run.status,
            "second_request_status": second_request.status,
            **first_snapshot,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["converged"] == 1
    assert result["run_status"] == "failed"
    assert result["run_error"] == LEASE_EXPIRED_ERROR
    assert result["request_status"] == "finished"
    assert result["enqueued"] == [result["relayed_run_id"]]
    assert result["relayed_status"] == "pending"
    assert result["second_request_status"] == "dispatched"
