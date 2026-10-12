"""工位 9 ToolGuard：副作用工具的幂等键与重复调用拦截（P4.5 落地）。

副作用判定读注册元数据（read_only=False 或 dangerous=True）；幂等键按
``toolguard:{run_id}:{tool}:{sha256(规范化入参)[:16]}`` 计算——同一 Run 内同工具同参
视为同一次副作用。执行中的同键调用拦下（DuplicateToolCallError，注册骨架转为模型可见
的错误结果并写失败事实）；已完成的同键调用不重复执行、复用既有结果（仍经 after_tool
复走打码链后进入会话状态）；执行失败释放键，允许重试。键经 ToolResult.metadata 交给
注册骨架写进 tool_audit：同键两行（仅复用行为 duration 0）即复用证据。

与持久层幂等的分工：本工位在内存里拦"同一 Run 内模型重复点名"；进程重启或 Run 恢复
重放由持久层幂等键兜底（订单列内唯一约束、凭证幂等键），两层都不可省。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.agents.middlewares.base import (
    DuplicateToolCallError,
    Station,
    ToolCallRequest,
    ToolResult,
)
from app.agents.middlewares.tool_approval import DANGEROUS_METADATA_KEY

READ_ONLY_METADATA_KEY = "read_only"
IDEMPOTENCY_KEY_METADATA_KEY = "idempotency_key"
KEY_PREFIX = "toolguard"


@dataclass(frozen=True)
class ToolGuardSettings:
    enabled: bool = True


def is_side_effect_call(call: ToolCallRequest) -> bool:
    """副作用判定：显式标为只读的调用不设防；dangerous 与写类工具都纳入。"""
    metadata = call.metadata or {}
    return metadata.get(READ_ONLY_METADATA_KEY) is False or bool(
        metadata.get(DANGEROUS_METADATA_KEY)
    )


def tool_guard_key(run_id: UUID | None, call: ToolCallRequest) -> str:
    """幂等键：Run + 工具名 + 规范化入参摘要；入参键序不影响键值。"""
    canonical = json.dumps(
        {"args": dict(call.args)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}:{run_id if run_id is not None else '-'}:{call.name}:{digest}"


class ToolGuardStation(Station):
    key = "tool_guard"
    label = "ToolGuard 副作用防护"

    def __init__(
        self, *, settings: ToolGuardSettings | None = None, run_id: UUID | None = None
    ) -> None:
        resolved = settings or ToolGuardSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._run_id = run_id
        self._in_flight: set[str] = set()
        self._completed: dict[str, ToolResult] = {}
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def key_for(self, call: ToolCallRequest) -> str | None:
        """副作用调用的幂等键；只读调用无键（不设防）。"""
        return tool_guard_key(self._run_id, call) if is_side_effect_call(call) else None

    async def before_tool(self, call: ToolCallRequest) -> None:
        key = self.key_for(call)
        if key is None or key in self._completed:
            return None
        if key in self._in_flight:
            self._events.append({"outcome": "blocked", "tool": call.name, "key": key})
            raise DuplicateToolCallError(call.name, key)
        self._in_flight.add(key)
        return None

    def reuse_tool_result(self, call: ToolCallRequest) -> ToolResult | None:
        key = self.key_for(call)
        if key is None or key not in self._completed:
            return None
        stored = self._completed[key]
        self._events.append({"outcome": "reused", "tool": call.name, "key": key})
        return ToolResult(
            call_id=call.call_id,
            content=stored.content,
            is_error=False,
            metadata={**stored.metadata, IDEMPOTENCY_KEY_METADATA_KEY: key},
        )

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        key = self.key_for(call)
        if key is None:
            return result
        self._in_flight.discard(key)
        result.metadata[IDEMPOTENCY_KEY_METADATA_KEY] = key
        if result.is_error:
            self._completed.pop(key, None)
            return result
        self._completed[key] = ToolResult(
            call_id=result.call_id, content=result.content, metadata=dict(result.metadata)
        )
        return result
