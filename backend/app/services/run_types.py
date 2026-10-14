"""Run 执行体契约类型：执行体与执行器共享，避免两侧互相导入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True)
class RunIdentity:
    run_id: UUID
    request_id: UUID
    thread_id: UUID
    user_id: UUID
    agent_slug: str


@dataclass(frozen=True)
class RunResult:
    token_usage: dict[str, object] | None = None
    langfuse_trace_id: str | None = None


@dataclass(frozen=True)
class RunInterrupted:
    """执行体在危险工具审批处挂起：Run 转 interrupted 等待人工决定，不写终态。

    `approvals` 为本次挂起的待审批元数据（call_id/工具/原因），执行器据此发 interrupt
    事件；审批事实已由执行体落库。"""

    approvals: list[dict[str, Any]] = field(default_factory=list)
