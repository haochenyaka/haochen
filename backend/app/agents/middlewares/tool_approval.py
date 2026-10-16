"""工位 11 工具审批：危险操作挂起等待人工审批，批准后执行、拒绝回执进对话。

启用（接线）后，`dangerous` 标记的工具在 `before_tool` 处经 LangGraph `interrupt()` 挂起：
图从检查点暂停、Run 转 `interrupted`、发 interrupt 事件；人工在管理端批准/拒绝后，恢复
入口以 `Command(resume={"approved": bool})` 续跑——批准则工具执行，拒绝则回一条拒绝
消息进对话（`ToolApprovalRejected`，模型据此改道或转人工）。

未接线时保持旧语义：`before_tool` 直接抛 `ToolApprovalRequired`（图将其收敛为失败），
供无图上下文的单元测试与未接入恢复入口的部署使用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.agents.middlewares.base import (
    Station,
    ToolApprovalRejected,
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
        interrupt_enabled: bool = False,
    ) -> None:
        resolved = settings or ToolApprovalSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._policy = policy or DangerousToolPolicy()
        self._interrupt_enabled = interrupt_enabled
        """是否走 interrupt 挂起：仅图有检查点（可 resume）时开启；否则回退失败语义。"""

    async def before_tool(self, call: ToolCallRequest) -> None:
        decision = self._policy.decide(call)
        if decision.action == "allow":
            return
        reason = decision.reason or "策略要求审批"
        if decision.action == "deny":
            logger.warning("工具审批拒绝：tool=%s reason=%s", call.name, reason)
            raise ToolDeniedError(call.name, reason)
        if not self._interrupt_enabled:
            # 无检查点（不可 resume）：旧语义——抛让图收敛为失败，不静默执行
            logger.warning("工具审批拦截（无挂起通道）：tool=%s", call.name)
            raise ToolApprovalRequired(call.name, reason)
        # 待审批：图内 interrupt 挂起（首次抛/冒泡、resume 时返回决定）；图外抛旧异常
        out_of_graph, result = _await_human_approval(call, reason)
        if out_of_graph:
            raise ToolApprovalRequired(call.name, reason)
        if _approved(result):
            logger.info("工具审批通过：tool=%s", call.name)
            return
        logger.warning("工具审批被拒（人工）：tool=%s", call.name)
        raise ToolApprovalRejected(call.name, _reason_of(result, reason))


def _await_human_approval(call: ToolCallRequest, reason: str) -> tuple[bool, Any]:
    """图内 interrupt 等待人工决定；返回 (是否图外, 决定)。

    - 图外（无 runnable 上下文）：interrupt 抛 `RuntimeError` → `(True, None)`，调用方抛旧
      异常走失败语义（单元测试与未接线部署）。
    - 图内首次：interrupt 抛 `GraphInterrupt` 冒泡，被 langgraph 捕获转成 `__interrupt__`，
      本函数不返回（节点被中断，审批元数据即 interrupt 载荷）。
    - 图内 resume：interrupt 返回人工决定 → `(False, 决定)`。
    """
    from langgraph.types import interrupt

    payload = {
        "type": "tool_approval",
        "tool": call.name,
        "call_id": call.call_id,
        "reason": reason,
        "args_digest": dict(call.args),
    }
    try:
        result = interrupt(payload)
    except RuntimeError:
        # "Called get_config outside of a runnable context"：图外调用
        return True, None
    return False, result


def _approved(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("approved"))
    return bool(result)


def _reason_of(result: Any, default: str) -> str:
    if isinstance(result, dict):
        reason = result.get("reason")
        if isinstance(reason, str) and reason:
            return reason
    return default
