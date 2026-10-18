"""Run 收敛时的助手回复提取：绝不把客人消息或工具消息落成助手回复。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.models.runtime import Message
from app.services.run_body import (
    HISTORY_TRUNCATED_NOTICE,
    build_invoke_input,
    final_reply,
)


def test_picks_last_ai_message_with_text() -> None:
    messages = [
        HumanMessage(content="你好"),
        AIMessage(content="第一句"),
        AIMessage(content="第二句"),
    ]
    assert final_reply(messages).content == "第二句"


def test_skips_empty_ai_and_tool_messages() -> None:
    messages = [
        AIMessage(content="有效答复"),
        AIMessage(content=""),
        ToolMessage(content="工具回执", tool_call_id="call-1"),
    ]
    assert final_reply(messages).content == "有效答复"


def test_raises_when_graph_produced_no_assistant_reply() -> None:
    """首轮 finish 的旧路径会把客人原话当回复落库；现在必须显式失败。"""
    with pytest.raises(RuntimeError, match="没有助手回复"):
        final_reply([HumanMessage(content="今天股市怎么样？")])


def _message(text: str) -> Message:
    return Message(thread_id=uuid4(), role="user", content={"text": text})


def test_truncated_history_tells_model_earlier_context_is_missing() -> None:
    payload = build_invoke_input([_message("最近一句")], truncated=True, resume=None)

    messages = payload["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == HISTORY_TRUNCATED_NOTICE
    assert isinstance(messages[-1], HumanMessage)


def test_complete_history_has_no_truncation_notice() -> None:
    payload = build_invoke_input([_message("只有一句")], truncated=False, resume=None)

    assert all(not isinstance(message, SystemMessage) for message in payload["messages"])


def test_resume_input_keeps_command_semantics() -> None:
    payload = build_invoke_input([], truncated=True, resume={"approved": True, "reason": ""})

    assert payload.resume == {"approved": True, "reason": ""}
