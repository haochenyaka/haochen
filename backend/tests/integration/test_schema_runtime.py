"""Schema 基线验证：表/列/唯一约束/部分索引与最小写入回路（真实 PostgreSQL + 迁移）。"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import AgentRun, AgentRunAttempt, Message, Request, Thread, User
from app.models.task import Task
from tests.integration.db_utils import inspect_tables, run_with_connection, run_with_session

pytestmark = pytest.mark.integration

RUNTIME_TABLES = {
    "users",
    "threads",
    "messages",
    "requests",
    "agent_runs",
    "agent_run_attempts",
    "tasks",
}


def test_runtime_tables_created(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    assert RUNTIME_TABLES | {"model_audit", "tool_audit"} <= set(tables)


def test_model_audit_has_cached_prompt_tokens(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    columns = tables["model_audit"]["columns"]
    assert "cached_prompt_tokens" in columns
    assert columns["cached_prompt_tokens"]["nullable"] is True


def test_unique_constraints_and_partial_indexes(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)

    assert "uq_agent_runs_request_id" in tables["agent_runs"]["unique_constraints"]
    assert "uq_agent_run_attempts_run_attempt" in tables["agent_run_attempts"]["unique_constraints"]

    task_indexes = tables["tasks"]["indexes"]
    dedupe = task_indexes["uq_tasks_dedupe_active"]
    assert dedupe["unique"] is True
    assert "dedupe_key IS NOT NULL" in str(dedupe["dialect_options"]["postgresql_where"])

    run_indexes = tables["agent_runs"]["indexes"]
    assert "running" in str(
        run_indexes["ix_agent_runs_running_lease"]["dialect_options"]["postgresql_where"]
    )
    assert "ix_agent_runs_status_created_at" in run_indexes

    assert "ix_requests_thread_status_created_at" in tables["requests"]["indexes"]
    assert "ix_messages_thread_created_at" in tables["messages"]["indexes"]


def test_insert_roundtrip_and_defaults(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        user = User(username="dev-tester", display_name="开发身份")
        session.add(user)
        await session.flush()

        thread = Thread(user_id=user.id, agent_slug="front-desk", title="schema 校验")
        session.add(thread)
        await session.flush()

        message = Message(
            thread_id=thread.id, role="user", content={"text": "你好"}, meta={"k": "v"}
        )
        session.add(message)
        await session.flush()

        request = Request(
            user_id=user.id,
            agent_slug="front-desk",
            thread_id=thread.id,
            message_id=message.id,
        )
        session.add(request)
        await session.flush()

        agent_run = AgentRun(
            request_id=request.id,
            thread_id=thread.id,
            user_id=user.id,
            agent_slug="front-desk",
        )
        session.add(agent_run)
        await session.flush()

        attempt = AgentRunAttempt(run_id=agent_run.id, attempt_no=1, owner_token="owner-a")
        session.add(attempt)
        await session.flush()

        task = Task(task_type="demo.echo", handler_version=1, payload={"n": 1})
        session.add(task)
        await session.commit()

        return {
            "user_id": user.id,
            "run_id": agent_run.id,
            "request_status": request.status,
            "run_status": agent_run.status,
            "run_attempt_count": agent_run.attempt_count,
            "task_status": task.status,
            "task_attempt_count": task.attempt_count,
            "task_cancel_requested": task.cancel_requested,
            "attempt_id": attempt.id,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["request_status"] == "queued"
    assert values["run_status"] == "pending"
    assert values["run_attempt_count"] == 0
    assert values["task_status"] == "pending"
    assert values["task_attempt_count"] == 0
    assert values["task_cancel_requested"] is False
    assert values["attempt_id"] > 0

    async def _read_back(session: AsyncSession) -> Any:
        return await session.get(AgentRun, values["run_id"])

    run = run_with_session(runtime_db_url, _read_back)
    assert run.status == "pending"
    assert run.owner_token is None
    assert run.lease_expires_at is None


def test_tasks_dedupe_active_partial_unique(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> str:
        session.add(Task(task_type="demo.echo", handler_version=1, payload={}, dedupe_key="k-1"))
        await session.commit()

        session.add(Task(task_type="demo.echo", handler_version=1, payload={}, dedupe_key="k-1"))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        first = (await session.execute(select(Task).where(Task.dedupe_key == "k-1"))).scalar_one()
        first.status = "succeeded"
        await session.commit()

        session.add(Task(task_type="demo.echo", handler_version=1, payload={}, dedupe_key="k-1"))
        await session.commit()

        rows = (await session.execute(select(Task).where(Task.dedupe_key == "k-1"))).scalars().all()
        return ",".join(sorted(row.status for row in rows))

    assert run_with_session(runtime_db_url, _body) == "pending,succeeded"
