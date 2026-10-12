"""工位 11 工具审批：危险操作默认拦截（业务审批策略随 P4+ 细化）。

默认策略下注册元数据标记 dangerous 的工具必须走审批；拒绝与待审批都在 Run 内显式
失败，不静默执行。审批等待的 interrupt/resume 语义（转人工参考）随 T9 接入。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from app.agents.middlewares.base import (
    Station,
    ToolApprovalRequired,
    ToolCallRequest,
    ToolDeniedError,
)

logger = logging.getLogger(__name__)

DANGEROUS_METADATA_KEY = "dangerous"

ApprovalAction = Literal["allow", "deny", "require_approval"]


@dataclass(frozen=True)
class ApprovalDecision:
    action: ApprovalAction
    reason: str = ""


@dataclass(frozen=True)
class ToolApprovalSettings:
    enabled: bool = True


class ApprovalPolicy(Protocol):
    """审批策略：按工具注册元数据与入参给出放行/拒绝/待审批。"""

    def decide(self, call: ToolCallRequest) -> ApprovalDecision: ...


class DangerousToolPolicy:
    """默认策略：dangerous 标记的工具待审批，其余放行。"""

    def decide(self, call: ToolCallRequest) -> ApprovalDecision:
        if call.metadata.get(DANGEROUS_METADATA_KEY):
            return ApprovalDecision("require_approval", "危险操作需人工审批")
        return ApprovalDecision("allow")


class AllowAllPolicy:
    def decide(self, call: ToolCallRequest) -> ApprovalDecision:
        return ApprovalDecision("allow")


class ToolApprovalStation(Station):
    key = "tool_approval"
    label = "工具审批"

    def __init__(
        self,
        *,
        settings: ToolApprovalSettings | None = None,
        policy: ApprovalPolicy | None = None,
    ) -> None:
        resolved = settings or ToolApprovalSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._policy = policy or DangerousToolPolicy()

    async def before_tool(self, call: ToolCallRequest) -> None:
        decision = self._policy.decide(call)
        if decision.action == "allow":
            return
        logger.warning(
            "工具审批拦截：tool=%s action=%s reason=%s", call.name, decision.action, decision.reason
        )
        if decision.action == "deny":
            raise ToolDeniedError(call.name, decision.reason or "策略拒绝")
        raise ToolApprovalRequired(call.name, decision.reason or "策略要求审批")
