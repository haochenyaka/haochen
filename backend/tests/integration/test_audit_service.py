"""审计埋点验证：事实独立事务提交、seq/用量/耗时落库、终态对账标记。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.audit import ModelAudit, ToolAudit
from app.repositories import AgentRunRepository, AuditRepository
from app.services.audit_service import (
    MODEL_STATUS_COMPLETED,
    TOOL_STATUS_COMPLETED,
    AuditRecorder,
    monotonic_ms,
)
from app.services.run_worker import RunContext, RunExecutor, RunResult
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import Intake, RecordingDispatcher, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


@dataclass
class AuditingBody:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, ctx: RunContext) -> RunResult | None:
        recorder = AuditRecorder(
            run_id=ctx.run_id,
            agent_slug=ctx.identity.agent_slug,
            session_factory=ctx.session_factory,
        )
        started = monotonic_ms()
        await recorder.model_call(
            actor_type="guest",
            actor_id=str(ctx.identity.user_id),
            model="stub-model",
            status=MODEL_STATUS_COMPLETED,
            duration_ms=monotonic_ms() - started,
            prompt_tokens=120,
            cached_prompt_tokens=96,
            completion_tokens=24,
            retrieval_refs=[{"chunk_id": "kb-1", "source": "faq"}],
        )
        await recorder.tool_call(
            tool_name="echo.lookup",
            status=TOOL_STATUS_COMPLETED,
            args_digest={"text": "***"},
            idempotency_key="idem-1",
            duration_ms=1,
        )
        self.entered.set()
        await self.release.wait()
        return None


async def _audit_state(factory: SessionFactoryArg, run_id: UUID) -> dict[str, Any]:
    async with factory() as session:
        model_rows = (
            (await session.execute(select(ModelAudit).where(ModelAudit.run_id == str(run_id))))
            .scalars()
            .all()
        )
        tool_rows = (
            (await session.execute(select(ToolAudit).where(ToolAudit.run_id == str(run_id))))
            .scalars()
            .all()
        )
        timeline = await AuditRepository(session).list_timeline(run_id=run_id)
    model = model_rows[0] if model_rows else None
    tool = tool_rows[0] if tool_rows else None
    return {
        "model_count": len(model_rows),
        "tool_count": len(tool_rows),
        "model_seq": model.seq if model else None,
        "model_prompt_tokens": model.prompt_tokens if model else None,
        "model_cached_tokens": model.cached_prompt_tokens if model else None,
        "model_completion_tokens": model.completion_tokens if model else None,
        "model_duration_ms": model.duration_ms if model else None,
        "model_retrieval_refs": model.retrieval_refs if model else None,
        "model_reconciled": model.state_reconciled if model else None,
        "tool_seq": tool.seq if tool else None,
        "tool_args_digest": tool.args_digest if tool else None,
        "tool_idempotency_key": tool.idempotency_key if tool else None,
        "timeline_kinds": sorted(entry.kind for entry in timeline.entries),
        "timeline_truncated": timeline.truncated,
    }


def test_recorder_facts_are_visible_before_run_terminal(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake: Intake = await intake_message(session, username="audit-recorder-tester")
        assert intake.run_id is not None

        body = AuditingBody()
        dispatcher = RecordingDispatcher()
        executor = RunExecutor(body, dispatcher=dispatcher, session_factory=factory)

        task = asyncio.create_task(executor.execute(intake.run_id))
        await body.entered.wait()
        mid_run = await _audit_state(factory, intake.run_id)
        body.release.set()
        outcome = await task
        async with factory() as session:
            run = await AgentRunRepository(session).get_by_id(intake.run_id)
        final = await _audit_state(factory, intake.run_id)
        return {
            "outcome": outcome,
            "mid_run": mid_run,
            "run_status": run.status if run else None,
            **{f"final_{key}": value for key, value in final.items()},
        }

    result = run_with_session_factory(runtime_db_url, _body)
    mid = result["mid_run"]
    assert mid["model_count"] == 1
    assert mid["tool_count"] == 1
    assert mid["model_seq"] == 1
    assert mid["tool_seq"] == 1
    assert mid["model_cached_tokens"] == 96
    assert mid["model_reconciled"] is False

    assert result["outcome"] == "completed"
    assert result["run_status"] == "completed"
    assert result["final_model_count"] == 1
    assert result["final_tool_count"] == 1
    assert result["final_model_prompt_tokens"] == 120
    assert result["final_model_cached_tokens"] == 96
    assert result["final_model_completion_tokens"] == 24
    assert result["final_model_duration_ms"] is not None
    assert result["final_model_retrieval_refs"] == [{"chunk_id": "kb-1", "source": "faq"}]
    assert result["final_model_reconciled"] is True
    assert result["final_tool_args_digest"] == {"text": "***"}
    assert result["final_tool_idempotency_key"] == "idem-1"
    assert result["final_timeline_kinds"] == ["model", "tool"]
    assert result["final_timeline_truncated"] is False


def test_recorder_seq_conflict_fails_explicitly(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            intake = await intake_message(session, username="audit-seq-tester")
        assert intake.run_id is not None
        recorder = AuditRecorder(
            run_id=intake.run_id, agent_slug="front-desk", session_factory=factory
        )
        await recorder.model_call(
            actor_type="system",
            actor_id="worker",
            model="stub-model",
            status=MODEL_STATUS_COMPLETED,
        )
        async with factory() as session:
            with pytest.raises(IntegrityError):
                await AuditRepository(session).append_model(
                    run_id=intake.run_id,
                    agent_slug="front-desk",
                    actor_type="system",
                    actor_id="worker",
                    model="stub-model",
                    status=MODEL_STATUS_COMPLETED,
                    seq=1,
                )
            await session.rollback()
        state = await _audit_state(factory, intake.run_id)
        return {"model_count": state["model_count"]}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["model_count"] == 1
