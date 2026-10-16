"""PR-0.5b 自证：危险工具挂起等待审批，批准后恢复执行、拒绝则回执进对话。

时序：Run 触发危险工具 → 挂起为 interrupted 并落一条待审批事实 → 人工决定 →
恢复入口投递同一 Run → claim_resume 认领 → 图从检查点以 Command(resume) 续跑。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.models.audit import ToolAudit
from app.models.runtime import Message, User
from app.repositories import AgentRunRepository, ToolApprovalRepository
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from app.services.tool_approval_service import ToolApprovalService
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


async def _admin_id(factory: SessionFactoryArg) -> Any:
    async with factory() as session:
        user = User(username=f"appr-admin-{uuid4().hex[:6]}", role="admin")
        session.add(user)
        await session.commit()
        return user.id


def _build_executor(registry: ToolRegistry, factory: SessionFactoryArg) -> RunExecutor:
    return RunExecutor(
        AgentRunBody(
            checkpointer=InMemorySaver(),
            resource_policy=StaticResourcePolicy(
                allowed=frozenset({("tool", "rotate_room_password")})
            ),
            tool_registry=registry,
        ),
        dispatcher=RecordingDispatcher(),
        session_factory=factory,
    )


def _dangerous_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @builtin_tool(category="test", dangerous=True, read_only=False, registry=registry)
    async def rotate_room_password(room: str) -> str:
        """危险操作：轮换房间门锁密码。"""
        return f"rotated:{room}"

    return registry


def test_approval_approved_resumes_and_executes_tool(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    from tests.integration.support import ScriptedToolChatModel

    registry = _dangerous_registry()
    model = ScriptedToolChatModel(tool_name="rotate_room_password", tool_args={"room": "A101"})
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("rotate_room_password",))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        executor = _build_executor(registry, factory)
        admin = await _admin_id(factory)
        async with factory() as session:
            intake = await intake_message(
                session, username=f"appr-{uuid4().hex[:8]}", text="换密码"
            )
        assert intake.run_id is not None

        suspend_outcome = await executor.execute(intake.run_id)
        async with factory() as session:
            run_after_suspend = await AgentRunRepository(session).get_by_id(intake.run_id)
            pending = await ToolApprovalRepository(session).get_pending_for_run(intake.run_id)
            assert pending is not None
            approval_id = pending.id
            approval_tool = pending.tool_name

        # 人工批准：服务层落决定并投递恢复
        async with factory() as session:
            await ToolApprovalService(session).decide(
                approval_id=approval_id,
                approved=True,
                decided_by=admin,
                dispatcher=RecordingDispatcher(),
            )
        # resume：执行器认领 interrupted Run，图从检查点续跑
        resume_outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            run_after = await AgentRunRepository(session).get_by_id(intake.run_id)
            approval_after = await ToolApprovalRepository(session).get_by_id(approval_id)
            tool_rows = (
                await session.execute(
                    select(func.count())
                    .select_from(ToolAudit)
                    .where(ToolAudit.run_id == str(intake.run_id))
                )
            ).scalar_one()
            assistant = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.thread_id == intake.thread_id, Message.role == "assistant")
                )
            ).scalar_one()
        return {
            "suspend_outcome": suspend_outcome,
            "suspend_status": run_after_suspend.status,
            "approval_tool": approval_tool,
            "resume_outcome": resume_outcome,
            "final_status": run_after.status,
            "approval_status": approval_after.status,
            "tool_rows": tool_rows,
            "assistant": assistant,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["suspend_outcome"] == "interrupted"
    assert result["suspend_status"] == "interrupted"
    assert result["approval_tool"] == "rotate_room_password"
    assert result["resume_outcome"] == "completed"
    assert result["final_status"] == "completed"
    assert result["approval_status"] == "consumed"
    # 批准后工具真正执行，且产生了面向客人的回复
    assert result["tool_rows"] == 1
    assert result["assistant"] == 1


def test_approval_denied_resumes_with_refusal_reply(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    from tests.integration.support import ScriptedToolChatModel

    registry = _dangerous_registry()
    model = ScriptedToolChatModel(tool_name="rotate_room_password", tool_args={"room": "A101"})
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("rotate_room_password",))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        executor = _build_executor(registry, factory)
        admin = await _admin_id(factory)
        async with factory() as session:
            intake = await intake_message(
                session, username=f"deny-{uuid4().hex[:8]}", text="换密码"
            )
        assert intake.run_id is not None

        suspend_outcome = await executor.execute(intake.run_id)
        async with factory() as session:
            pending = await ToolApprovalRepository(session).get_pending_for_run(intake.run_id)
            assert pending is not None
            approval_id = pending.id

        async with factory() as session:
            await ToolApprovalService(session).decide(
                approval_id=approval_id,
                approved=False,
                decided_by=admin,
                dispatcher=RecordingDispatcher(),
            )
        resume_outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            run_after = await AgentRunRepository(session).get_by_id(intake.run_id)
            tool_rows = (
                await session.execute(
                    select(func.count())
                    .select_from(ToolAudit)
                    .where(ToolAudit.run_id == str(intake.run_id))
                )
            ).scalar_one()
            replies = (
                (
                    await session.execute(
                        select(Message).where(
                            Message.thread_id == intake.thread_id, Message.role == "assistant"
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {
            "suspend_outcome": suspend_outcome,
            "resume_outcome": resume_outcome,
            "final_status": run_after.status,
            "tool_rows": tool_rows,
            "reply_text": str(replies[-1].content.get("text")) if replies else "",
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["suspend_outcome"] == "interrupted"
    # 拒绝后 Run 正常完成（非失败），工具未执行，回复携带拒绝回执
    assert result["resume_outcome"] == "completed"
    assert result["final_status"] == "completed"
    assert result["tool_rows"] == 1  # 拒绝也记一条失败事实
    assert "未通过" in result["reply_text"]
