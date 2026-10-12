"""Run 执行体契约类型：执行体与执行器共享，避免两侧互相导入。"""

from __future__ import annotations

from dataclasses import dataclass
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
