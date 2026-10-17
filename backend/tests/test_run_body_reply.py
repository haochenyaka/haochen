"""Run 收敛时的助手回复提取：绝不把客人消息或工具消息落成助手回复。"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services.run_body import final_reply


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
