"""P4 CredentialGuard 集成验证：真实 Run 里明文不进消息库、检查点、审计与投影事实。

模型替身在工具回包与答复里都带上明文密码，落库产物与检查点状态必须是打码形态；
生成中的流式增量不审查（下发与提醒场景的实时回复可含当日密码），该边界在用例里显式钉住。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.middlewares.credential_guard import MASK_TEXT, scan_sensitive_plaintext
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.models.audit import ToolAudit
from app.repositories import MessageRepository
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import (
    RecordingDispatcher,
    RecordingRunEventBus,
    ScriptedToolChatModel,
    force_stub_model,
    intake_message,
)

pytestmark = pytest.mark.integration

GUEST = "credential-guest"
TOOL_NAME = "lock_issue_credential"
ROOM_NO = "8302"
PASSWORD = "839201"


class LeakyToolChatModel(ScriptedToolChatModel):
    """第二次调用把密码写进答复：助手消息入库前必须被打码。"""

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        reply = result.generations[0].message
        if reply.tool_calls:
            return result
        leak = AIMessage(content=f"提醒：您的门锁密码：{PASSWORD}，请于今日使用。")
        return ChatResult(generations=[ChatGeneration(message=leak)])


def test_run_keeps_plaintext_out_of_persisted_artifacts(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def lock_issue_credential(room_no: str, password: str = "") -> str:
        """签发房间凭证（测试替身：回包带门锁服务下发的明文载荷）。"""
        return f"room_no={room_no} password={password} 门锁密码：{password}"

    model = LeakyToolChatModel(
        tool_name=TOOL_NAME, tool_args={"room_no": ROOM_NO, "password": PASSWORD}
    )
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", (TOOL_NAME,))

    async def _body(factory: Any) -> dict[str, Any]:
        bus = RecordingRunEventBus()
        checkpointer = InMemorySaver()
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=checkpointer,
                resource_policy=StaticResourcePolicy(allowed=frozenset({("tool", TOOL_NAME)})),
                tool_registry=registry,
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        async with factory() as session:
            intake = await intake_message(session, username=GUEST, text="房间密码是多少")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        persisted: list[tuple[str, str]] = []
        reply: str | None = None
        audit_args: dict[str, Any] = {}
        async with factory() as session:
            for message in await MessageRepository(session).list_for_thread(intake.thread_id):
                text = json.dumps(message.content, ensure_ascii=False)
                persisted.append((f"message:{message.role}", text))
                if message.role == "assistant":
                    reply = str(message.content.get("text"))
            rows = (
                (
                    await session.execute(
                        select(ToolAudit).where(ToolAudit.run_id == str(intake.run_id))
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                audit_args = dict(row.args_digest or {})
                persisted.append(
                    (
                        "tool_audit",
                        json.dumps(
                            {"tool": row.tool_name, "args": row.args_digest, "error": row.error},
                            ensure_ascii=False,
                        ),
                    )
                )

        state = await checkpointer.aget_tuple(
            {"configurable": {"thread_id": str(intake.thread_id)}}
        )
        state_messages: list[BaseMessage] = (
            [] if state is None else state.checkpoint["channel_values"]["messages"]
        )
        for message in state_messages:
            persisted.append(
                (f"state:{message.type}", json.dumps(str(message.content), ensure_ascii=False))
            )

        return {
            "outcome": outcome,
            "reply": reply,
            "audit_args": audit_args,
            "tool_rows": len(rows),
            "persisted": persisted,
            "state_types": [message.type for message in state_messages],
            "events": [
                (f"event:{record.event}", json.dumps(record.data, ensure_ascii=False))
                for record in bus.records(intake.run_id)
            ],
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["tool_rows"] == 1
    # 助手答复入库前被打码：模型复述的明文只存在于生成时刻
    assert result["reply"] == f"提醒：您的门锁密码：{MASK_TEXT}，请于今日使用。"
    # 审计入参按键名整值打码（模型生成的入参不在审计里留明文）
    assert result["audit_args"] == {"room_no": ROOM_NO, "password": MASK_TEXT}
    assert "tool" in result["state_types"]
    for source, text in result["persisted"]:
        assert scan_sensitive_plaintext(text) == [], f"敏感明文进入持久化产物：{source}"
    for source, text in result["persisted"]:
        if source == "state:tool":
            assert f"password={PASSWORD}" not in text and MASK_TEXT in text
    # 投影事实（消息完成事件）同样不带明文
    completed = next(
        text for source, text in result["events"] if source == "event:message.completed"
    )
    assert PASSWORD not in completed
    # 工具调用事件带的是打码后的入参副本
    called = next(text for source, text in result["events"] if source == "event:tool.call")
    assert PASSWORD not in called
    # 边界钉住：打码落在持久化边界上，生成中的流式增量不审查——下发与提醒场景的实时回复
    # 可以带当日密码，但同一内容的落库副本（上一条断言）必定是打码形态
    deltas = [text for source, text in result["events"] if source == "event:message.delta"]
    assert any(PASSWORD in text for text in deltas)
