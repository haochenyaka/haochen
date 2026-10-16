"""Supervisor 图状态：对话消息 + 用量/摘要事件。

与子 Agent 回环子图（`LoopState`）在 messages / token_usage / summarization_events
三个通道上兼容，子图可作为节点直接嵌进本图。路由决策不进状态：状态随检查点跨 Run 残留，
把"已派发路由"放进来会让下一轮 Run 误判为已处理而直接收尾；派发痕迹改由装配期闭包持有
（图每次 Run 重建，闭包随之复位），见 `build_supervisor_graph`。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage


class SupervisorState(TypedDict):
    messages: list[BaseMessage]
    token_usage: dict[str, object] | None
    summarization_events: Annotated[list[dict[str, Any]], operator.add]


__all__ = ["SupervisorState"]
