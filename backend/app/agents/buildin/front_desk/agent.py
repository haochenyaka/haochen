"""front-desk 前台入口（supervisor）：把客人诉求路由到咨询/订单/客房服务/转人工子 Agent。

对外契约与单 Agent 时期一致：入口仍是本 Agent 的编译图，输入历史消息、输出含 messages
与 token_usage；差别只在图内部是多层拓扑——supervisor 决策节点按诉求派发子图，子图产出后
回到 supervisor 决定是否继续派发。管理端对本 slug 的 system_prompt 覆盖作用于默认子 Agent
（咨询），模型与温度作用于全图。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents.base import BaseAgent
from app.agents.context import BaseContext, config_item
from app.agents.models import build_chat_model
from app.agents.runtime import RuntimeContext
from app.agents.supervisor.assembly import build_agent_pipeline
from app.agents.supervisor.graph import SUPERVISOR_TOOL_IDS, build_supervisor_graph

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

DEFAULT_SYSTEM_PROMPT = (
    "你是无人酒店的前台助理，负责回答客人的咨询与房型、预订相关问题。"
    "回答简洁、礼貌；不确定的信息不要编造，无法处理时建议转人工。"
    "需要当前时间等信息时调用工具获取，不凭记忆作答。"
)

HOTEL_KNOWLEDGE_BASE = "hotel-guest"
"""宾客知识库代码：与知识库种子的 KB_CODE 保持一致（漂移由测试兜底）。"""


@dataclass
class FrontDeskContext(BaseContext):
    system_prompt: str = field(
        default=DEFAULT_SYSTEM_PROMPT,
        metadata=config_item(label="系统提示词", kind="textarea", group="prompt"),
    )
    model: str = field(
        default="",
        metadata=config_item(label="模型名（留空用全局默认）", group="model"),
    )
    temperature: float = field(
        default=0.2,
        metadata=config_item(label="采样温度", kind="number", group="model"),
    )


class FrontDeskAgent(BaseAgent):
    slug = "front-desk"
    name = "前台助理"
    description = "面向客人的前台入口（supervisor）：咨询、订单、客房服务与转人工的分派与协作"
    context_schema = FrontDeskContext
    default_system_prompt = DEFAULT_SYSTEM_PROMPT
    tool_ids = SUPERVISOR_TOOL_IDS
    """supervisor 声明的工具并集（子 Agent 声明的合取）；各子图再按声明取子集。"""
    knowledge_base_ids = (HOTEL_KNOWLEDGE_BASE,)
    skill_ids = ("checkin-flow",)

    async def build_graph(self, runtime: RuntimeContext) -> CompiledStateGraph[Any, Any, Any, Any]:
        context = FrontDeskContext.from_config(runtime.config)
        model = build_chat_model(
            config={"model": context.model, "temperature": context.temperature}
        )
        pipeline = build_agent_pipeline(runtime)
        return await build_supervisor_graph(
            runtime,
            model=model,
            pipeline=pipeline,
            default_agent_prompt=context.system_prompt,
        )


__all__ = ["DEFAULT_SYSTEM_PROMPT", "HOTEL_KNOWLEDGE_BASE", "FrontDeskAgent", "FrontDeskContext"]
