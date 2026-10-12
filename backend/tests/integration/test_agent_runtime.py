"""Agent 运行体验证：历史重放、检查点落库、审计留痕与模型失败语义。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.postgres.base import BasePostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.models import STUB_MODEL_NAME, STUB_REPLY_PREFIX
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.config import get_settings
from app.models.audit import ModelAudit, ToolAudit
from app.models.runtime import AgentRunAttempt, Message
from app.services.audit_service import (
    MODEL_STATUS_COMPLETED,
    MODEL_STATUS_FAILED,
    TOOL_STATUS_COMPLETED,
)
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import (
    RecordingDispatcher,
    ScriptedToolChatModel,
    force_stub_model,
    intake_message,
)

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

CHECKPOINT_TABLES = (
    "checkpoint_migrations",
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


def _postgres_dsn(db_url: str) -> str:
    return make_url(db_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def _open_saver(
    db_url: str,
) -> tuple[AsyncConnectionPool[AsyncConnection[DictRow]], AsyncPostgresSaver]:
    """按 Alembic 迁移建好的表接入检查点：不调用 setup()，运行期不建表。"""
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=_postgres_dsn(db_url),
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    return pool, AsyncPostgresSaver(pool)


async def _reply_texts(factory: SessionFactoryArg, thread_id: UUID) -> list[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                select(Message).where(Message.thread_id == thread_id).order_by(Message.seq)
            )
        ).scalars()
        return [str(row.content.get("text")) for row in rows if row.role == "assistant"]


def test_agent_run_body_replays_history_and_writes_checkpoint(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        pool, checkpointer = await _open_saver(runtime_db_url)
        try:
            dispatcher = RecordingDispatcher()
            executor = RunExecutor(
                AgentRunBody(checkpointer=checkpointer),
                dispatcher=dispatcher,
                session_factory=factory,
            )
            async with factory() as session:
                first = await intake_message(
                    session, username="agent-runtime-tester", text="有哪些房型"
                )
            assert first.run_id is not None
            first_outcome = await executor.execute(first.run_id)

            async with factory() as session:
                second = await intake_message(
                    session,
                    username="agent-runtime-tester",
                    text="大床房多少钱",
                    thread_id=first.thread_id,
                )
            assert second.run_id is not None
            second_outcome = await executor.execute(second.run_id)

            async with factory() as session:
                audits = (
                    (
                        await session.execute(
                            select(ModelAudit)
                            .where(ModelAudit.run_id == str(first.run_id))
                            .order_by(ModelAudit.seq)
                        )
                    )
                    .scalars()
                    .all()
                )
                attempts = (
                    (
                        await session.execute(
                            select(AgentRunAttempt)
                            .where(AgentRunAttempt.run_id == second.run_id)
                            .order_by(AgentRunAttempt.attempt_no)
                        )
                    )
                    .scalars()
                    .all()
                )
                checkpoint_rows = (
                    await session.execute(
                        text("SELECT count(*) FROM checkpoints WHERE thread_id = :tid"),
                        {"tid": str(first.thread_id)},
                    )
                ).scalar_one()

            state = await checkpointer.aget_tuple(
                {"configurable": {"thread_id": str(first.thread_id)}}
            )
            state_messages = [] if state is None else state.checkpoint["channel_values"]["messages"]
            return {
                "first_outcome": first_outcome,
                "second_outcome": second_outcome,
                "replies": await _reply_texts(factory, first.thread_id),
                "audit_rows": [
                    (
                        row.model,
                        row.status,
                        row.prompt_tokens,
                        row.cached_prompt_tokens,
                        row.completion_tokens,
                        row.state_reconciled,
                    )
                    for row in audits
                ],
                "second_attempt_outcome": attempts[0].outcome if attempts else None,
                "second_attempt_finished": attempts[0].finished_at is not None
                if attempts
                else None,
                "checkpoint_rows": checkpoint_rows,
                "state_message_count": len(state_messages),
                "enqueued": dispatcher.enqueued,
            }
        finally:
            await pool.close()

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["first_outcome"] == "completed"
    assert result["second_outcome"] == "completed"
    assert result["replies"] == [
        f"{STUB_REPLY_PREFIX}有哪些房型",
        f"{STUB_REPLY_PREFIX}大床房多少钱",
    ]
    # 历史重放：第二轮图状态包含两轮完整对话（4 条消息），检查点按线程留存
    assert result["state_message_count"] == 4
    assert result["checkpoint_rows"] >= 2
    assert result["second_attempt_outcome"] == "completed"
    assert result["second_attempt_finished"] is True
    model, status, prompt, cached, completion, reconciled = result["audit_rows"][0]
    assert (model, status, cached, reconciled) == (
        STUB_MODEL_NAME,
        MODEL_STATUS_COMPLETED,
        0,
        True,
    )
    assert prompt is not None and prompt > 0
    assert completion is not None and completion > 0


def test_checkpoint_tables_are_migration_managed(runtime_db_url: str) -> None:
    """建表收在 Alembic：水位已达最新，运行期 setup() 不执行任何 DDL。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            present = (
                await session.execute(
                    text("SELECT count(*) FROM pg_class WHERE relname = any(:tables)"),
                    {"tables": list(CHECKPOINT_TABLES)},
                )
            ).scalar_one()
            watermark = (
                await session.execute(text("SELECT max(v) FROM checkpoint_migrations"))
            ).scalar_one()

        pool, saver = await _open_saver(runtime_db_url)
        try:
            await saver.setup()
        finally:
            await pool.close()

        async with factory() as session:
            after_setup = (
                await session.execute(text("SELECT max(v) FROM checkpoint_migrations"))
            ).scalar_one()
        return {
            "tables_present": present,
            "watermark": watermark,
            "after_setup": after_setup,
            "latest_ddl_version": len(BasePostgresSaver.MIGRATIONS) - 1,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["tables_present"] == len(CHECKPOINT_TABLES)
    assert result["watermark"] == result["latest_ddl_version"]
    assert result["after_setup"] == result["watermark"]


def test_run_summarizes_history_and_records_event_in_checkpoint(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实 Run 走完整流水线：超阈值时压缩请求、摘要事件进检查点、数据库消息不删。"""
    force_stub_model(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "summary_threshold", 1)
    monkeypatch.setattr(settings, "summary_keep_messages", 1)
    monkeypatch.setattr(settings, "workdir_root", str(tmp_path / "workdir"))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
        executor = RunExecutor(
            AgentRunBody(checkpointer=checkpointer),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            first = await intake_message(
                session, username="agent-summary-tester", text="有哪些房型"
            )
        assert first.run_id is not None
        first_outcome = await executor.execute(first.run_id)

        async with factory() as session:
            second = await intake_message(
                session,
                username="agent-summary-tester",
                text="大床房多少钱",
                thread_id=first.thread_id,
            )
        assert second.run_id is not None
        second_outcome = await executor.execute(second.run_id)

        state = await checkpointer.aget_tuple({"configurable": {"thread_id": str(first.thread_id)}})
        async with factory() as session:
            audits = (
                (
                    await session.execute(
                        select(ModelAudit)
                        .where(ModelAudit.run_id == str(second.run_id))
                        .order_by(ModelAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
            message_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.thread_id == first.thread_id)
                )
            ).scalar_one()
        channel_values = {} if state is None else state.checkpoint["channel_values"]
        return {
            "first_outcome": first_outcome,
            "second_outcome": second_outcome,
            "replies": await _reply_texts(factory, first.thread_id),
            "second_audit_models": [row.model for row in audits],
            "second_audit_statuses": [row.status for row in audits],
            "message_count": message_count,
            "events": channel_values.get("summarization_events", []),
            "token_usage": channel_values.get("token_usage"),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["first_outcome"] == "completed"
    assert result["second_outcome"] == "completed"
    assert result["replies"] == [
        f"{STUB_REPLY_PREFIX}有哪些房型",
        f"{STUB_REPLY_PREFIX}大床房多少钱",
    ]
    # 第二轮 = 摘要调用 + 回复调用，两次都写模型审计事实
    assert result["second_audit_models"] == [STUB_MODEL_NAME, STUB_MODEL_NAME]
    assert result["second_audit_statuses"] == [MODEL_STATUS_COMPLETED, MODEL_STATUS_COMPLETED]
    # 压缩不改数据库消息：两轮共 4 条
    assert result["message_count"] == 4

    events = result["events"]
    assert len(events) == 1
    event = events[0]
    assert event["cutoff"] == 2
    assert str(event["summary"]).startswith(STUB_REPLY_PREFIX)
    history_file = Path(str(event["history_file"]))
    assert history_file.exists()
    payload = json.loads(history_file.read_text(encoding="utf-8"))
    assert [item["role"] for item in payload] == ["客人", "助手"]
    assert result["token_usage"] is not None
    assert result["token_usage"]["model_calls"] == 2


def test_model_failure_leaves_audit_fact_and_fails_run(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingModel:
        model_name = "boom-model"

        def bind_tools(self, tools: Any) -> Any:
            return self

        async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
            raise RuntimeError("模型不可用")

    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: ExplodingModel(),
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "model_retry_backoff_seconds", 0.0)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        async with factory() as session:
            intake = await intake_message(session, username="agent-failure-tester")
        assert intake.run_id is not None
        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            run = (
                await session.execute(
                    select(AgentRunAttempt).where(AgentRunAttempt.run_id == intake.run_id)
                )
            ).scalar_one()
            audits = (
                (
                    await session.execute(
                        select(ModelAudit)
                        .where(ModelAudit.run_id == str(intake.run_id))
                        .order_by(ModelAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
            assistant_count = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.thread_id == intake.thread_id, Message.role == "assistant")
                )
            ).scalar_one()
        return {
            "outcome": outcome,
            "attempt_outcome": run.outcome,
            "attempt_error_class": run.error_class,
            "attempt_count": len(audits),
            "audit_statuses": [row.status for row in audits],
            "audit_error": audits[0].error if audits else None,
            "audit_model": audits[0].model if audits else None,
            "audit_reconciled": audits[0].state_reconciled if audits else None,
            "assistant_count": assistant_count,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "failed"
    assert result["attempt_outcome"] == "failed"
    assert result["attempt_error_class"] == "RuntimeError"
    # ModelRetry 每次尝试都是一次真实调用：默认 3 次尝试各留一条失败事实
    assert result["attempt_count"] == 3
    assert set(result["audit_statuses"]) == {MODEL_STATUS_FAILED}
    assert result["audit_model"] == "boom-model"
    assert "模型不可用" in (result["audit_error"] or "")
    assert result["audit_reconciled"] is True
    assert result["assistant_count"] == 0


def test_run_executes_registered_tool_and_records_facts(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实 Run 工具回环：授权工具对模型可见且可执行，事实入 tool_audit，未授权工具不可见。"""
    force_stub_model(monkeypatch)
    executed: list[str] = []
    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def test_echo(text: str) -> str:
        """回显文本。"""
        executed.append(text)
        return f"echo:{text}"

    @builtin_tool(category="test", read_only=False, registry=registry)
    async def secret_tool() -> str:
        """未授权工具。"""
        executed.append("secret")
        return "secret"

    model = ScriptedToolChatModel(tool_name="test_echo")
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("test_echo", "secret_tool"))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langchain_core.messages import ToolMessage
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=checkpointer,
                resource_policy=StaticResourcePolicy(allowed=frozenset({("tool", "test_echo")})),
                tool_registry=registry,
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            intake = await intake_message(session, username="agent-tool-tester", text="现在几点")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        state = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": str(intake.thread_id)}}
        )
        async with factory() as session:
            tool_rows = (
                (
                    await session.execute(
                        select(ToolAudit)
                        .where(ToolAudit.run_id == str(intake.run_id))
                        .order_by(ToolAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
            model_count = (
                await session.execute(
                    select(func.count())
                    .select_from(ModelAudit)
                    .where(ModelAudit.run_id == str(intake.run_id))
                )
            ).scalar_one()
        channel_values = {} if state is None else state.checkpoint["channel_values"]
        return {
            "outcome": outcome,
            "replies": await _reply_texts(factory, intake.thread_id),
            "executed": list(executed),
            "tool_rows": [
                (row.tool_name, row.status, row.duration_ms, row.args_digest, row.error)
                for row in tool_rows
            ],
            "model_count": model_count,
            "seen_tools": model.seen_tools,
            "tool_messages": [
                str(message.content)
                for message in channel_values.get("messages", [])
                if isinstance(message, ToolMessage)
            ],
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    # 工具只执行一次，入参与授权集合一致：未授权的 secret_tool 既不可见也不执行
    assert result["executed"] == ["你好"]
    assert result["replies"] == ["最终回复：echo:你好"]
    assert result["seen_tools"] == [["test_echo"], ["test_echo"]]
    assert result["model_count"] == 2
    assert result["tool_messages"] == ["echo:你好"]
    assert len(result["tool_rows"]) == 1
    tool_name, status, duration_ms, args_digest, error = result["tool_rows"][0]
    assert tool_name == "test_echo"
    assert status == TOOL_STATUS_COMPLETED
    assert isinstance(duration_ms, int) and duration_ms >= 0
    assert args_digest == {"text": "你好"}
    assert error is None
