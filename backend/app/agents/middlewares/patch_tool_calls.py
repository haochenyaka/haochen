"""工位 7 PatchToolCalls：修正工具调用消息形态。

历史重放与截断（摘要折叠、外部写入）会留下悬空 tool_calls 或孤立 ToolMessage，
OpenAI 兼容接口会直接报 400。统一修正：悬空调用从助手消息的 tool_calls 中剔除
（无文本且无剩余调用的整条移除），孤立与重复的 ToolMessage 整条移除。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.agents.middlewares.base import ModelCallRequest, Station


@dataclass(frozen=True)
class PatchToolCallsSettings:
    enabled: bool = True


def patch_tool_calls(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    declared = {
        str(call.get("id"))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call.get("id")
    }
    responded = {
        str(message.tool_call_id)
        for message in messages
        if isinstance(message, ToolMessage) and str(message.tool_call_id) in declared
    }
    patched: list[BaseMessage] = []
    answered: set[str] = set()
    for message in messages:
        if isinstance(message, ToolMessage):
            call_id = str(message.tool_call_id)
            if call_id in declared and call_id not in answered:
                answered.add(call_id)
                patched.append(message)
            continue
        if isinstance(message, AIMessage) and message.tool_calls:
            remaining = [call for call in message.tool_calls if str(call.get("id")) in responded]
            if len(remaining) == len(message.tool_calls):
                patched.append(message)
                continue
            if remaining or _has_text(message):
                patched.append(message.model_copy(update={"tool_calls": remaining}))
            continue
        patched.append(message)
    return patched


def _has_text(message: AIMessage) -> bool:
    content: Any = message.content
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


class PatchToolCallsStation(Station):
    key = "patch_tool_calls"
    label = "PatchToolCalls 消息形态修正"

    def __init__(self, *, settings: PatchToolCallsSettings | None = None) -> None:
        resolved = settings or PatchToolCallsSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        request.messages = patch_tool_calls(request.messages)
        return request
