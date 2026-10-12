"""Repository 层验证：Run 租约/attempt/终态收敛、FIFO 队列守卫、审计 seq 与时间线。"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models.runtime import AgentRun, AgentRunAttempt, Thread
from app.repositories import (
    LEASE_EXPIRED_ERROR,
    AgentRunRepository,
    AuditRepository,
    MessageRepository,
    RequestRepository,
    ThreadRepository,
    UserRepository,
)
from tests.integration.db_utils import run_with_engine, run_with_session

pytestmark = pytest.mark.integration


@dataclass
class Seed:
    user_id: UUID
    thread_id: UUID
    message_id: UUID
    request_id: UUID
    run_id: UUID


async def _seed(session: AsyncSession, username: str) -> Seed:
    user = await UserRepository(session).get_or_create(username=username)
    thread = await ThreadRepository(session).create(user_id=user.id, agent_slug="front-desk")
    message = await MessageRepository(session).append(
        thread_id=thread.id, role="user", content={"text": "你好"}
    )
    request = await RequestRepository(session).create(
        user_id=user.id, agent_slug="front-desk", thread_id=thread.id, message_id=message.id
    )
    run = await AgentRunRepository(session).create_pending(
        request_id=request.id, thread_id=thread.id, user_id=user.id, agent_slug="front-desk"
    )
    return Seed(
        user_id=user.id,
        thread_id=thread.id,
        message_id=message.id,
        request_id=request.id,
        run_id=run.id,
    )


def test_claim_transitions_pending_to_running_with_attempt(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "claim-tester")
        runs = AgentRunRepository(session)

        attempt = await runs.claim(seed.run_id, owner_token="worker-1", lease_seconds=60)
        assert attempt is not None
        assert attempt.attempt_no == 1
        assert attempt.owner_token == "worker-1"
        assert attempt.lease_expires_at is not None

        run = await runs.get_by_id(seed.run_id)
        assert run is not None and run.status == "running"
        assert run.attempt_count == 1
        assert run.owner_token == "worker-1"
        assert run.lease_expires_at is not None

        second = await runs.claim(seed.run_id, owner_token="worker-2", lease_seconds=60)
        assert second is None
        return {"status": run.status}

    assert run_with_session(runtime_db_url, _body)["status"] == "running"


def test_heartbeat_and_terminal_guarded_by_owner(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "terminal-tester")
        runs = AgentRunRepository(session)
        await runs.claim(seed.run_id, owner_token="worker-1", lease_seconds=60)

        assert await runs.heartbeat(seed.run_id, owner_token="other", lease_seconds=60) is False
        assert await runs.heartbeat(seed.run_id, owner_token="worker-1", lease_seconds=120) is True

        assert (
            await runs.write_terminal(seed.run_id, owner_token="other", status="completed") is False
        )
        assert (
            await runs.write_terminal(
                seed.run_id,
                owner_token="worker-1",
                status="completed",
                token_usage={"prompt_tokens": 10},
            )
            is True
        )
        assert (
            await runs.write_terminal(seed.run_id, owner_token="worker-1", status="completed")
            is False
        )

        run = await runs.get_by_id(seed.run_id)
        assert run is not None
        attempt = (
            await session.execute(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == seed.run_id)
            )
        ).scalar_one()
        return {
            "status": run.status,
            "owner": run.owner_token,
            "lease": run.lease_expires_at,
            "finished": run.finished_at is not None,
            "token_usage": run.token_usage,
            "attempt_outcome": attempt.outcome,
            "attempt_finished": attempt.finished_at is not None,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["status"] == "completed"
    assert result["owner"] is None
    assert result["lease"] is None
    assert result["finished"] is True
    assert result["token_usage"] == {"prompt_tokens": 10}
    assert result["attempt_outcome"] == "completed"
    assert result["attempt_finished"] is True


def test_reconcile_expired_lease_converges_failed(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "reconcile-tester")
        runs = AgentRunRepository(session)
        await runs.claim(seed.run_id, owner_token="worker-dead", lease_seconds=-5)

        candidates = await runs.list_expired_lease_candidates()
        assert [candidate[0] for candidate in candidates] == [seed.run_id]

        converged = await runs.converge_expired([candidate[0] for candidate in candidates])
        assert converged == [seed.run_id]
        assert await runs.converge_expired([seed.run_id]) == []
        assert await runs.list_expired_lease_candidates() == []

        run = await runs.get_by_id(seed.run_id)
        assert run is not None
        attempt = (
            await session.execute(
                select(AgentRunAttempt).where(AgentRunAttempt.run_id == seed.run_id)
            )
        ).scalar_one()
        return {
            "status": run.status,
            "error": run.error,
            "lease": run.lease_expires_at,
            "attempt_outcome": attempt.outcome,
            "attempt_error_class": attempt.error_class,
            "attempt_finished": attempt.finished_at is not None,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["status"] == "failed"
    assert result["error"] == LEASE_EXPIRED_ERROR
    assert result["lease"] is None
    assert result["attempt_outcome"] == "lease_expired"
    assert result["attempt_error_class"] == LEASE_EXPIRED_ERROR
    assert result["attempt_finished"] is True


def test_cancel_paths(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        runs = AgentRunRepository(session)

        pending_seed = await _seed(session, "cancel-pending-tester")
        assert await runs.cancel_pending(pending_seed.run_id) is True
        assert await runs.cancel_pending(pending_seed.run_id) is False
        assert (
            await runs.claim(pending_seed.run_id, owner_token="worker-1", lease_seconds=60) is None
        )
        cancelled_run = await runs.get_by_id(pending_seed.run_id)

        running_seed = await _seed(session, "cancel-running-tester")
        await runs.claim(running_seed.run_id, owner_token="worker-1", lease_seconds=60)
        assert await runs.request_cancel(running_seed.run_id) is True
        assert await runs.request_cancel(running_seed.run_id) is False
        requested_run = await runs.get_by_id(running_seed.run_id)
        assert requested_run is not None
        requested_status = requested_run.status
        assert await runs.write_terminal(
            running_seed.run_id, owner_token="worker-1", status="cancelled"
        )
        final_run = await runs.get_by_id(running_seed.run_id)

        assert cancelled_run is not None and final_run is not None
        return {
            "cancelled_status": cancelled_run.status,
            "cancelled_finished": cancelled_run.finished_at is not None,
            "requested_status": requested_status,
            "final_status": final_run.status,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["cancelled_status"] == "cancelled"
    assert result["cancelled_finished"] is True
    assert result["requested_status"] == "cancel_requested"
    assert result["final_status"] == "cancelled"


def test_fifo_head_position_and_dispatch_guards(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "fifo-tester")
        requests = RequestRepository(session)
        messages = MessageRepository(session)

        second = await requests.create(
            user_id=seed.user_id,
            agent_slug="front-desk",
            thread_id=seed.thread_id,
            message_id=(
                await messages.append(
                    thread_id=seed.thread_id, role="user", content={"text": "第二条"}
                )
            ).id,
        )
        third = await requests.create(
            user_id=seed.user_id,
            agent_slug="front-desk",
            thread_id=seed.thread_id,
            message_id=(
                await messages.append(
                    thread_id=seed.thread_id, role="user", content={"text": "第三条"}
                )
            ).id,
        )

        head = await requests.get_head_queued(seed.thread_id)
        positions = [
            await requests.queued_position(seed.request_id),
            await requests.queued_position(second.id),
            await requests.queued_position(third.id),
        ]

        assert await requests.mark_dispatched(seed.request_id) is True
        assert await requests.mark_dispatched(seed.request_id) is False
        assert await requests.mark_cancelled(seed.request_id) is False

        next_head = await requests.get_head_queued(seed.thread_id)
        assert await requests.mark_cancelled(second.id) is True
        assert await requests.mark_finished(seed.request_id) is True
        third_head = await requests.get_head_queued(seed.thread_id)
        head_after = await requests.get_by_id(third.id)
        finished = await requests.get_by_id(seed.request_id)
        cancelled = await requests.get_by_id(second.id)

        assert head is not None and next_head is not None and third_head is not None
        assert finished is not None and cancelled is not None and head_after is not None
        return {
            "head_is_first": head.id == seed.request_id,
            "positions": positions,
            "next_head_is_second": next_head.id == second.id,
            "third_head_is_third": third_head.id == third.id,
            "finished_status": finished.status,
            "cancelled_status": cancelled.status,
            "cancelled_finished": cancelled.finished_at is not None,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["head_is_first"] is True
    assert result["positions"] == [1, 2, 3]
    assert result["next_head_is_second"] is True
    assert result["third_head_is_third"] is True
    assert result["finished_status"] == "finished"
    assert result["cancelled_status"] == "cancelled"
    assert result["cancelled_finished"] is True


def test_thread_row_lock_is_exclusive(runtime_db_url: str) -> None:
    async def _body(engine: AsyncEngine) -> bool:
        async with AsyncSession(engine, expire_on_commit=False) as setup:
            seed = await _seed(setup, "lock-tester")
            await setup.commit()

        async with (
            AsyncSession(engine, expire_on_commit=False) as holder,
            AsyncSession(engine, expire_on_commit=False) as contender,
        ):
            async with holder.begin():
                locked = await ThreadRepository(holder).get_for_update(seed.thread_id)
                assert locked is not None
                with pytest.raises(DBAPIError):
                    async with contender.begin():
                        await contender.execute(
                            select(Thread)
                            .where(Thread.id == seed.thread_id)
                            .with_for_update(nowait=True)
                        )
        return True

    assert run_with_engine(runtime_db_url, _body) is True


def test_audit_seq_isolation_and_timeline(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "audit-tester")
        audits = AuditRepository(session)
        run_key = str(seed.run_id)

        first = await audits.append_model(
            run_id=seed.run_id,
            agent_slug="front-desk",
            actor_type="guest",
            actor_id=str(seed.user_id),
            model="fake-chat",
            status="ok",
            duration_ms=12,
            prompt_tokens=100,
            cached_prompt_tokens=60,
            completion_tokens=20,
        )
        second = await audits.append_model(
            run_id=seed.run_id,
            agent_slug="front-desk",
            actor_type="guest",
            actor_id=str(seed.user_id),
            model="fake-chat",
            status="error",
            error="boom",
        )
        tool_row = await audits.append_tool(
            run_id=seed.run_id,
            agent_slug="front-desk",
            tool_name="echo",
            status="ok",
            idempotency_key="key-1",
        )

        await session.commit()
        with pytest.raises(IntegrityError):
            await audits.append_model(
                run_id=seed.run_id,
                agent_slug="front-desk",
                actor_type="guest",
                actor_id=str(seed.user_id),
                model="fake-chat",
                status="ok",
                seq=first.seq,
            )
        await session.rollback()

        timeline = await audits.list_timeline(run_id=seed.run_id)
        by_run = await audits.list_timeline(thread_id=seed.thread_id)
        limited = await audits.list_timeline(run_id=seed.run_id, limit=2)
        reconciled = await audits.mark_run_reconciled(seed.run_id)
        with pytest.raises(ValueError):
            await audits.list_timeline()

        assert first.seq == 1 and second.seq == 2 and tool_row.seq == 1
        return {
            "run_key": run_key,
            "model_seqs": [first.seq, second.seq],
            "tool_seq": tool_row.seq,
            "timeline_kinds": [entry.kind for entry in timeline.entries],
            "timeline_truncated": timeline.truncated,
            "by_run_count": len(by_run.entries),
            "limited_count": len(limited.entries),
            "limited_truncated": limited.truncated,
            "reconciled_rows": reconciled,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["model_seqs"] == [1, 2]
    assert result["tool_seq"] == 1
    assert sorted(result["timeline_kinds"]) == ["model", "model", "tool"]
    assert result["timeline_truncated"] is False
    assert result["by_run_count"] == 3
    assert result["limited_count"] == 2
    assert result["limited_truncated"] is True
    assert result["reconciled_rows"] == 2


def test_audit_cached_tokens_and_state_flags_persist(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        seed = await _seed(session, "audit-flags-tester")
        audits = AuditRepository(session)
        await audits.append_model(
            run_id=seed.run_id,
            agent_slug="front-desk",
            actor_type="guest",
            actor_id=str(seed.user_id),
            model="fake-chat",
            status="ok",
            prompt_tokens=100,
            cached_prompt_tokens=60,
            completion_tokens=20,
        )
        await audits.mark_run_reconciled(seed.run_id)
        timeline = await audits.list_timeline(run_id=seed.run_id)
        entry = timeline.entries[0]
        row = await session.get(AgentRun, seed.run_id)
        assert row is not None
        return {
            "kind": entry.kind,
            "cached": entry.cached_prompt_tokens,
            "prompt": entry.prompt_tokens,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["kind"] == "model"
    assert result["prompt"] == 100
    assert result["cached"] == 60
