"""Run 事件协议：SSE 事件名、事件包络与每 Run 发射器。

事件流是运行状态的投影，不是事实来源：消息、审计与终态事实以数据库为准，消息与
终态事件在数据库提交之后发射。事件名对齐开发计划附录 B（run.started、message.delta、
tool.call、tool.result、message.completed、run.completed、run.failed、handoff.requested、
interrupt），其中 run.cancelled 用于投影取消终态；handoff.requested 在 P1 只保留事件位
（业务转人工随 P7.8 接入）。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from app.services.run_event_bus import RunEventBus, RunEventRecord

logger = logging.getLogger(__name__)

EVENT_RUN_STARTED = "run.started"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_TOOL_CALL = "tool.call"
EVENT_TOOL_RESULT = "tool.result"
EVENT_MESSAGE_COMPLETED = "message.completed"
EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_RUN_CANCELLED = "run.cancelled"
EVENT_RUN_INTERRUPTED = "run.interrupted"
EVENT_HANDOFF_REQUESTED = "handoff.requested"
EVENT_INTERRUPT = "interrupt"

TERMINAL_EVENT_TYPES: frozenset[str] = frozenset(
    {EVENT_RUN_COMPLETED, EVENT_RUN_FAILED, EVENT_RUN_CANCELLED, EVENT_RUN_INTERRUPTED}
)

ERROR_TEXT_LIMIT = 500

_TERMINAL_EVENT_BY_STATUS = {
    "completed": EVENT_RUN_COMPLETED,
    "failed": EVENT_RUN_FAILED,
    "cancelled": EVENT_RUN_CANCELLED,
    "interrupted": EVENT_RUN_INTERRUPTED,
}


def terminal_event_type(status: str) -> str | None:
    """终态状态 → 终态事件名；非终态返回 None。"""
    return _TERMINAL_EVENT_BY_STATUS.get(status)


def format_sse_event(record: RunEventRecord) -> str:
    """SSE 线格式：id/event/data 三段 + 空行；data 为单行 JSON（保留中文）。"""
    payload = json.dumps(record.data, ensure_ascii=False, default=str)
    return f"id: {record.id}\nevent: {record.event}\ndata: {payload}\n\n"


class RunEventEmitter:
    """每 Run 一个发射器：统一包络（run_id/thread_id/agent_slug/seq），失败不影响运行。

    seq 为发射器内自增序号（同一 Run 的重试尝试各自从 1 开始），只用于消费侧排序参考；
    断线续传以传输游标（RunEventRecord.id）为准。
    """

    def __init__(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        agent_slug: str,
        bus: RunEventBus | None = None,
        on_first_token: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._run_id = run_id
        self._thread_id = thread_id
        self._agent_slug = agent_slug
        self._bus = bus
        self._on_first_token = on_first_token
        self._first_token_recorded = False
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

    async def emit(self, event_type: str, **data: Any) -> None:
        """发射事件；发布失败只告警（投影可缺失，运行与事实落库不受影响）。"""
        self._seq += 1
        if self._bus is None:
            return
        payload = {
            "run_id": str(self._run_id),
            "thread_id": str(self._thread_id),
            "agent_slug": self._agent_slug,
            "seq": self._seq,
            **data,
        }
        try:
            await self._bus.publish(self._run_id, event_type, payload)
        except Exception:
            logger.warning(
                "run event publish failed: run_id=%s event=%s",
                self._run_id,
                event_type,
                exc_info=True,
            )

    async def run_started(self) -> None:
        await self.emit(EVENT_RUN_STARTED)

    async def message_delta(self, text: str) -> None:
        await self._record_first_token()
        await self.emit(EVENT_MESSAGE_DELTA, text=text)

    async def _record_first_token(self) -> None:
        """首个增量到达时触发一次首 token 埋点（TTFT）；失败只告警，不影响运行与投影。"""
        if self._first_token_recorded or self._on_first_token is None:
            self._first_token_recorded = True
            return
        self._first_token_recorded = True
        try:
            await self._on_first_token()
        except Exception:
            logger.warning("first token recording failed: run_id=%s", self._run_id, exc_info=True)

    async def message_completed(self, *, text: str, message_id: UUID | str | None = None) -> None:
        await self.emit(
            EVENT_MESSAGE_COMPLETED,
            text=text,
            message_id=None if message_id is None else str(message_id),
        )

    async def tool_call(self, *, tool: str, call_id: str, args: Mapping[str, Any]) -> None:
        await self.emit(EVENT_TOOL_CALL, tool=tool, call_id=call_id, args=dict(args))

    async def tool_result(
        self,
        *,
        tool: str,
        call_id: str,
        status: str,
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        await self.emit(
            EVENT_TOOL_RESULT,
            tool=tool,
            call_id=call_id,
            status=status,
            duration_ms=duration_ms,
            error=_truncate(error),
        )

    async def run_finished(
        self,
        *,
        status: str,
        error: str | None = None,
        error_class: str | None = None,
        token_usage: Mapping[str, Any] | None = None,
    ) -> None:
        event_type = terminal_event_type(status) or EVENT_RUN_FAILED
        await self.emit(
            event_type,
            status=status,
            error=_truncate(error),
            error_class=error_class,
            token_usage=dict(token_usage) if token_usage else None,
        )

    async def interrupt(self, *, tool: str, call_id: str, reason: str) -> None:
        """工具审批等待事件位：等待回答/审批的语义随 interrupt/resume（P2+）补全。"""
        await self.emit(EVENT_INTERRUPT, tool=tool, call_id=call_id, reason=_truncate(reason))

    async def handoff_requested(self, *, reason: str, detail: str | None = None) -> None:
        """转人工事件位：P1 无生产方，业务 handoff（P7.8）接入后由业务节点发射。"""
        await self.emit(EVENT_HANDOFF_REQUESTED, reason=reason, detail=detail)


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    return text[:ERROR_TEXT_LIMIT]
