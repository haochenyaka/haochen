"""客房服务子 Agent：清洁、送物与机器人配送派发（supervisor 子 Agent）。"""

from __future__ import annotations

from app.agents.supervisor.assembly import LoopSubAgent

DEFAULT_SYSTEM_PROMPT = (
    "你是无人酒店的客房服务助理，负责清洁、送物与机器人配送的派发。"
    "派发前确认房号与需要的物品或服务，派发成功后如实告知客人已受理；"
    "工具没有给出送达时间就不要编时间，也不夸大承诺；"
    "工具返回结果原样转述，失败时说明原因，不编造成功。"
)


class RoomServiceAgent(LoopSubAgent):
    slug = "room-service"
    name = "客房服务"
    description = "清洁、送物与机器人配送派发（supervisor 子 Agent）"
    default_system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_ids = (
        "request_robot_delivery",
        "request_room_clean",
    )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "RoomServiceAgent"]
