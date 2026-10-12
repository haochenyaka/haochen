"""工位 10 TokenUsage：近似上下文 + Provider 实际用量。

近似口径与摘要阈值共用同一估算函数（CJK 按 1 字符 1 token、其余 4 字符 1 token），
只用于阈值判断与报表口径；Provider 实际用量（含缓存命中）在模型返回后逐次累计，
Run 终态由装配层写入 AgentRun.token_usage。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agents.middlewares.base import ModelCallDelegate, ModelCallRequest, Station
from app.agents.models import ModelUsage, extract_usage

CJK_PATTERN = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
MESSAGE_OVERHEAD_TOKENS = 4


@dataclass(frozen=True)
class TokenUsageSettings:
    enabled: bool = True


def estimate_text_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(CJK_PATTERN.findall(text))
    return cjk + (len(text) - cjk + 3) // 4


def estimate_message_tokens(message: BaseMessage) -> int:
    total = MESSAGE_OVERHEAD_TOKENS + estimate_text_tokens(_content_text(message.content))
    for call in getattr(message, "tool_calls", None) or ():
        total += estimate_text_tokens(str(call.get("name", "")))
        total += estimate_text_tokens(
            json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
        )
    return total


def estimate_messages_tokens(messages: Sequence[BaseMessage]) -> int:
    return sum(estimate_message_tokens(message) for message in messages)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


class TokenUsageStation(Station):
    key = "token_usage"
    label = "TokenUsage 用量统计"

    def __init__(self, *, settings: TokenUsageSettings | None = None) -> None:
        resolved = settings or TokenUsageSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._usages: list[ModelUsage] = []
        self._approx_context_tokens = 0
        self._model_calls = 0

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        self._approx_context_tokens = max(
            self._approx_context_tokens, estimate_messages_tokens(request.messages)
        )
        return request

    async def call_model(self, request: ModelCallRequest, call: ModelCallDelegate) -> AIMessage:
        reply = await call()
        self.record_usage(reply)
        return reply

    def record_usage(self, reply: AIMessage) -> None:
        """记录一次实际模型调用的用量：内部压缩等旁路调用也计入 Run 总量。"""
        self._usages.append(extract_usage(reply))
        self._model_calls += 1

    def summary(self) -> dict[str, object]:
        """Run 用量汇总：Provider 实际用量 + 上下文峰值 + 调用次数。"""
        return {
            "prompt_tokens": _sum_optional(usage.prompt_tokens for usage in self._usages),
            "cached_prompt_tokens": _sum_optional(
                usage.cached_prompt_tokens for usage in self._usages
            ),
            "completion_tokens": _sum_optional(usage.completion_tokens for usage in self._usages),
            "approx_context_tokens": self._approx_context_tokens,
            "model_calls": self._model_calls,
        }


def _sum_optional(values: Any) -> int | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None
