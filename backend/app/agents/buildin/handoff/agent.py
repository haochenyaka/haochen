"""转人工子 Agent：投诉、退款、赔偿、人身安全等诉求（supervisor 子 Agent）。

承接会话侧的转人工链路：`request_handoff` 生成本会话的待处理工单（按线程幂等），
`submit_complaint` 另落投诉事实（供统计与质检）；两者分工见 AGENTS.md。
"""

from __future__ import annotations

from app.agents.supervisor.assembly import LoopSubAgent

DEFAULT_SYSTEM_PROMPT = (
    "你是无人酒店的转人工助理，负责投诉、退款、赔偿、人身安全等需要人工介入的诉求。"
    "先安抚客人并如实说明将转交人工处理、会尽快联系；用 request_handoff 生成转人工工单，"
    "涉及投诉时另用 submit_complaint 记录诉求事实；"
    "不自行承诺退款金额与到账时间，不推断处理结果。"
)


class HandoffAgent(LoopSubAgent):
    slug = "handoff"
    name = "转人工"
    description = "投诉、退款、赔偿等诉求转人工（supervisor 子 Agent）"
    default_system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_ids = ("request_handoff", "submit_complaint")


__all__ = ["DEFAULT_SYSTEM_PROMPT", "HandoffAgent"]
