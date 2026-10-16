"""转人工工具集成测试（真实 PostgreSQL）：Run 链路生成待处理工单，按线程幂等。

工具层断言以数据库事实为准：`request_handoff` 落 `HandoffRequest`（pending），同一会话重复
调用复用既有工单不产生第二行；最后一个用例走完整 Run 链路（图 → 工具 → 工单 → tool_audit），
验证"模型 → 工具 → 事实"闭环，即 handoff 子 Agent 真正接上了会话侧的转人工链路。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.context import ResourceSnapshot
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import default_registry
from app.models.audit import ToolAudit
from app.models.conversation import HANDOFF_STATUS_PENDING, HandoffRequest
from app.repositories import HandoffRequestRepository, ThreadRepository, UserRepository
from app.services.audit_service import AuditRecorder
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, ScriptedToolChatModel, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


async def _call(tool_name: str, context: RuntimeContext | None, **kwargs: Any) -> str:
    tool = default_registry().get(tool_name)
    runtime = SimpleNamespace(context=context, tool_call_id="call-1")
    return await tool.coroutine(runtime=runtime, **kwargs)


def test_request_handoff_tool_reuses_pending_ticket(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user = await UserRepository(session).get_or_create(
                username=f"handoff-tool-{uuid4().hex[:8]}"
            )
            thread = await ThreadRepository(session).create(
                user_id=user.id, agent_slug="front-desk"
            )
            await session.commit()
            context = RuntimeContext(
                agent_slug="front-desk",
                user_id=user.id,
                thread_id=thread.id,
                run_id=uuid4(),
                actor_id="handoff-tool-test",
                resources=ResourceSnapshot(),
                config={},
                prompt_docs="",
                checkpointer=None,
                recorder=AuditRecorder(
                    run_id=uuid4(), agent_slug="front-desk", session_factory=factory
                ),
                session_factory=factory,
            )
            first = await _call("request_handoff", context, reason="申请退款到账")
            second = await _call("request_handoff", context, reason="申请退款到账")
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(HandoffRequest)
                    .where(HandoffRequest.thread_id == thread.id)
                )
            ).scalar_one()
            pending = await HandoffRequestRepository(session).get_pending_for_thread(thread.id)
        return {
            "first": first,
            "second": second,
            "count": count,
            "status": pending.status if pending else None,
            "reason": pending.reason if pending else None,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["count"] == 1
    assert values["status"] == HANDOFF_STATUS_PENDING
    assert values["reason"] == "申请退款到账"
    assert "工单已生成" in values["first"]
    assert "已有待处理工单" in values["second"]


def test_run_handoff_creates_pending_ticket_and_records_tool_audit(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整 Run 链路：投诉诉求路由到 handoff → 模型点名 request_handoff → 落单并留痕。"""
    username = f"handoff-loop-{uuid4().hex[:8]}"
    model = ScriptedToolChatModel(
        tool_name="request_handoff", tool_args={"reason": "投诉房间噪音影响休息"}
    )
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        executor = RunExecutor(
            AgentRunBody(
                checkpointer=InMemorySaver(),
                resource_policy=StaticResourcePolicy(
                    allowed=frozenset({("tool", "request_handoff")})
                ),
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            intake = await intake_message(
                session, username=username, text="我要投诉，房间太吵了，根本没法休息"
            )
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            pending = await HandoffRequestRepository(session).get_pending_for_thread(
                intake.thread_id
            )
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
        return {
            "outcome": outcome,
            "pending": pending,
            "tool_rows": [(row.tool_name, row.status) for row in tool_rows],
            "seen_tools": model.seen_tools,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    assert values["pending"] is not None
    assert values["pending"].status == HANDOFF_STATUS_PENDING
    assert values["pending"].reason == "投诉房间噪音影响休息"
    assert values["tool_rows"] == [("request_handoff", "completed")]
    # 授权快照只放行 request_handoff：handoff 子 Agent 的可见工具随之收敛
    assert ["request_handoff"] in values["seen_tools"]
