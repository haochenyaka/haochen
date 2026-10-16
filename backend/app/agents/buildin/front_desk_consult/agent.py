"""前台咨询子 Agent：知识问答、房型与时间等咨询（supervisor 子 Agent）。

面向客人的通用咨询入口，也是 supervisor 的默认派发目标；工具以只读检索为主，
写类与客服类事务交给订单/客房服务/转人工子 Agent。
"""

from __future__ import annotations

from app.agents.supervisor.assembly import LoopSubAgent

DEFAULT_SYSTEM_PROMPT = (
    "你是无人酒店的前台咨询助理，负责回答客人的咨询、房型与时间相关问题。"
    '回答简洁、礼貌，称客人为"您"；涉及事实先用 query_kb 检索资料再回答，'
    '涉及"今天、明天、现在"的时间判断先用 get_current_time 获取，不凭记忆作答；'
    "房型与价格用 list_room_types 查询。不确定的信息不要编造，无法处理时建议转人工。"
)

HOTEL_KNOWLEDGE_BASE = "hotel-guest"
"""宾客知识库代码：与知识库种子的 KB_CODE 保持一致（漂移由测试兜底）。"""


class FrontDeskConsultAgent(LoopSubAgent):
    slug = "front-desk-consult"
    name = "前台咨询"
    description = "知识问答、房型介绍与时间等咨询（supervisor 子 Agent）"
    default_system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_ids = (
        "get_current_time",
        "list_kbs",
        "query_kb",
        "list_room_types",
        "get_checkin_info",
        "deliver_room_password",
    )
    knowledge_base_ids = (HOTEL_KNOWLEDGE_BASE,)
    skill_ids = ("checkin-flow",)


__all__ = ["DEFAULT_SYSTEM_PROMPT", "HOTEL_KNOWLEDGE_BASE", "FrontDeskConsultAgent"]
