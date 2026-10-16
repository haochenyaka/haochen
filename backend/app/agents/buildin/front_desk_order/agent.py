"""订单子 Agent：预订、改期、续订、取消与发票申请（supervisor 子 Agent）。

写类工具多，装配期靠中间件流水线的 ToolGuard 保证幂等、ToolApproval 处理危险调用。
"""

from __future__ import annotations

from app.agents.supervisor.assembly import LoopSubAgent

DEFAULT_SYSTEM_PROMPT = (
    "你是无人酒店的订单助理，负责预订、改期、续订、取消与发票申请。"
    "调用写类工具前必须与客人确认关键信息（房型、日期、人数、发票抬头），不要自行猜测；"
    "工具返回的订单信息要原样转述，失败时如实说明原因，不编造成功；"
    "涉及退款到账、发票红冲等资金诉求时先转人工确认，不自行承诺。"
)


class FrontDeskOrderAgent(LoopSubAgent):
    slug = "front-desk-order"
    name = "订单助理"
    description = "预订、改期、续订、取消与发票申请（supervisor 子 Agent）"
    default_system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_ids = (
        "list_room_types",
        "list_my_orders",
        "create_order",
        "reschedule_order",
        "renew_order",
        "cancel_order",
        "apply_invoice",
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "FrontDeskOrderAgent"]
