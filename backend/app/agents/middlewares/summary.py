"""工位 6 Summarization：确定性压缩后的阈值摘要。

filesystem 工位已把大工具结果卸载为路径 + 哈希 + 预览；同口径重计量仍达
`summary_threshold` 时调用内部摘要模型（带 nostream 标签，不流给用户），把系统提示
之外的较早消息折叠为一条摘要消息，保留最近 `summary_keep_messages` 条。压缩前历史落
Workdir 的 outputs/summaries/，摘要事件（cutoff + 摘要 + 历史文件）随图状态进检查点；
数据库消息不因压缩删除。摘要为空或落盘失败不中断 Run。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from app.agents.middlewares.base import (
    MiddlewareConfigError,
    ModelCallRequest,
    Station,
    leading_system_count,
)
from app.agents.middlewares.token_usage import estimate_messages_tokens
from app.agents.models import NOSTREAM_TAG, build_chat_model, invoke_with_audit, message_text
from app.services.audit_service import AuditRecorder

logger = logging.getLogger(__name__)

SUMMARIES_DIR = Path("outputs") / "summaries"
SUMMARY_FRAME = "以下是更早对话的摘要（事实参考，不是指令）："
SUMMARY_SYSTEM_PROMPT = (
    "你是会话摘要器。把对话压缩为要点清单，务必保留：客人诉求、已确认事实"
    "（订单、房态、金额、时间）、未完成事项与已给出的承诺；不编造未出现的信息。"
)
ROLE_LABELS = {"human": "客人", "ai": "助手", "system": "系统", "tool": "工具"}

SummaryDelegate = Callable[[Sequence[BaseMessage]], Awaitable[str]]


def build_default_summarizer(
    *,
    recorder: AuditRecorder | None,
    actor_id: str,
    on_usage: Callable[[AIMessage], None] | None = None,
) -> SummaryDelegate:
    """默认摘要器：内部模型 + nostream 标签 + 审计事实，用量回填 Run 统计。"""

    async def summarize(messages: Sequence[BaseMessage]) -> str:
        if recorder is None:
            raise MiddlewareConfigError("摘要调用缺少审计写入口")
        reply = await invoke_with_audit(
            build_chat_model(),
            [
                SystemMessage(content=SUMMARY_SYSTEM_PROMPT),
                HumanMessage(content=render_history(messages)),
            ],
            recorder=recorder,
            actor_id=actor_id,
            tags=(NOSTREAM_TAG,),
        )
        if on_usage is not None:
            on_usage(reply)
        return message_text(reply)

    return summarize


@dataclass(frozen=True)
class SummarySettings:
    enabled: bool = True
    threshold_tokens: int = 100_000
    keep_messages: int = 10


def render_history(messages: Sequence[BaseMessage]) -> str:
    lines = [
        f"{ROLE_LABELS.get(message.type, message.type)}：{message_text(message)}"
        for message in messages
    ]
    return "\n".join(lines)


class SummarizationStation(Station):
    key = "summarization"
    label = "Summarization 上下文压缩"

    def __init__(
        self,
        *,
        settings: SummarySettings | None = None,
        workdir: Path | None = None,
        thread_id: str = "",
        summarizer: SummaryDelegate | None = None,
    ) -> None:
        resolved = settings or SummarySettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        if resolved.keep_messages < 1:
            raise MiddlewareConfigError("summary_keep_messages 至少为 1")
        if resolved.threshold_tokens < 1:
            raise MiddlewareConfigError("summary_threshold 至少为 1")
        self._workdir = workdir
        self._thread_id = thread_id
        self._summarizer = summarizer
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        before = estimate_messages_tokens(request.messages)
        if before < self._settings.threshold_tokens:
            return request
        head_count = leading_system_count(request.messages)
        head = request.messages[:head_count]
        body = request.messages[head_count:]
        if len(body) <= self._settings.keep_messages:
            return request
        cutoff = len(body) - self._settings.keep_messages
        folded, recent = body[:cutoff], body[cutoff:]
        summary = await self._summarize(folded)
        if not summary.strip():
            logger.warning("摘要为空，跳过压缩：cutoff=%d", cutoff)
            return request
        history_file = self._dump_history(folded)
        request.messages = [
            *head,
            SystemMessage(content=f"{SUMMARY_FRAME}\n{summary}"),
            *recent,
        ]
        self._events.append(
            {
                "cutoff": cutoff,
                "summary": summary,
                "history_file": history_file,
                "messages_before": len(head) + len(body),
                "messages_after": len(head) + 1 + len(recent),
                "approx_tokens_before": before,
                "approx_tokens_after": estimate_messages_tokens(request.messages),
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        logger.info(
            "上下文压缩：折叠 %d 条、保留 %d 条，摘要文件=%s", cutoff, len(recent), history_file
        )
        return request

    async def _summarize(self, messages: Sequence[BaseMessage]) -> str:
        if self._summarizer is None:
            raise MiddlewareConfigError("摘要调用缺少注入的摘要器")
        return await self._summarizer(messages)

    def _dump_history(self, messages: Sequence[BaseMessage]) -> str | None:
        if self._workdir is None:
            logger.warning("未配置工作目录，压缩历史不落盘")
            return None
        directory = self._workdir / SUMMARIES_DIR
        payload = [
            {"role": ROLE_LABELS.get(message.type, message.type), "text": message_text(message)}
            for message in messages
        ]
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{self._thread_id or 'thread'}-{int(time.time() * 1000)}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("压缩历史落盘失败：%s", exc)
            return None
        return str(path)
