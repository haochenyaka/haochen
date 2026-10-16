"""Supervisor 拓扑装配：决策节点 + 子 Agent 子图，子图返回后回到 supervisor 汇总。

对外契约不变：图入口仍是"按 slug 取 Agent 编译图"，输入 `{messages}`、输出含 `messages`
与 `token_usage`（见 `AgentRunBody`）。supervisor 首轮判定派发目标，各子 Agent 子图作为
节点嵌入；子图产出后回到 supervisor 决定是否继续派发，`finish` 收尾。派发次数有上限
（`MAX_DISPATCHES`），避免决策与派发互相触发而空转。

派发痕迹由装配期闭包持有，而非图状态：图每次 Run 重建（`build_graph` 每个 Run 调一次），
闭包随之复位；放进状态会随检查点跨 Run 残留，导致下一轮误判为已处理。

授权感知派发：决策路由在当前资源快照下无可见工具、而其他子 Agent 有工具时改派后者，避免
工具被裁后仍派给无工具的子 Agent 而空转。未被子 Agent 声明的工具（快照里多出来的）归默认
子 Agent（咨询），保证探针类工具的注入行为与单 Agent 时期一致。

归属与事实：决策节点以 `SUPERVISOR_SLUG` 记录器写路由事实（判定值、判定来源、实际派发
目标），各子图以自身 slug 派生记录器写模型与工具事实——归属在装配期确定，不依赖运行期
上下文传递。决策本身若调用真实模型（结构化输出），其模型事实同样归到 `SUPERVISOR_SLUG`，
用量经流水线的旁路口径计入 Run 汇总；该调用不注入记忆/技能/steer（路由提示词只有调度指令）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from app.agents.buildin.front_desk_consult.agent import FrontDeskConsultAgent
from app.agents.buildin.front_desk_order.agent import FrontDeskOrderAgent
from app.agents.buildin.handoff.agent import HandoffAgent
from app.agents.buildin.room_service.agent import RoomServiceAgent
from app.agents.runtime import RuntimeContext
from app.agents.supervisor.assembly import LoopSubAgent
from app.agents.supervisor.routing import (
    DEFAULT_ROUTE,
    MAX_DISPATCHES,
    ROUTE_CONSULT,
    ROUTE_FINISH,
    ROUTE_HANDOFF,
    ROUTE_ORDER,
    ROUTE_ROOM_SERVICE,
    SUB_AGENT_ROUTES,
    RouteName,
    SupervisorRouter,
)
from app.agents.supervisor.state import SupervisorState

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langgraph.graph.state import CompiledStateGraph

    from app.agents.middlewares import MiddlewarePipeline

SUB_AGENT_TYPES: dict[str, type[LoopSubAgent]] = {
    ROUTE_CONSULT: FrontDeskConsultAgent,
    ROUTE_ORDER: FrontDeskOrderAgent,
    ROUTE_ROOM_SERVICE: RoomServiceAgent,
    ROUTE_HANDOFF: HandoffAgent,
}
"""路由 → 子 Agent 类型（与路由常量一一对应，见 `SUB_AGENT_ROUTES`）。"""

SUPERVISOR_SLUG = "supervisor"
"""决策节点自身的审计归属：路由事实与子 Agent 的模型/工具事实据此区分归属。"""

SUPERVISOR_TOOL_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(
        tool_id for agent_type in SUB_AGENT_TYPES.values() for tool_id in agent_type.tool_ids
    )
)
"""supervisor 声明的工具并集：运行入口按此构建授权快照，子 Agent 再按声明取子集。"""


async def build_supervisor_graph(
    runtime: RuntimeContext,
    *,
    model: BaseChatModel,
    pipeline: MiddlewarePipeline,
    default_agent_prompt: str | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """装配 supervisor 图；default_agent_prompt 覆盖默认子 Agent（咨询）的系统提示词。"""
    available = runtime.resources.ids("tool")
    claimed: set[str] = set()
    for agent_type in SUB_AGENT_TYPES.values():
        claimed.update(agent_type.tool_ids)
    unclaimed: tuple[str, ...] = tuple(name for name in available if name not in claimed)

    route_recorder = runtime.recorder.with_agent(SUPERVISOR_SLUG)
    router = SupervisorRouter(
        model,
        recorder=route_recorder,
        actor_id=runtime.actor_id,
        on_usage=pipeline.record_side_channel_usage,
    )
    builder: StateGraph[SupervisorState, RuntimeContext, SupervisorState, SupervisorState] = (
        StateGraph(SupervisorState, context_schema=RuntimeContext)
    )

    dispatched: list[RouteName] = []
    tool_counts: dict[str, int] = {}
    for route in SUB_AGENT_ROUTES:
        agent = SUB_AGENT_TYPES[route]()
        extra: Sequence[str] = unclaimed if route == DEFAULT_ROUTE else ()
        visible = agent.visible_tools(runtime, extra)
        tool_counts[route] = len(visible)
        override = default_agent_prompt if route == DEFAULT_ROUTE else None
        subgraph = agent.build_subgraph(
            runtime,
            model=model,
            pipeline=pipeline,
            system_prompt=agent.compose_system_prompt(override),
            tools=visible,
        )
        builder.add_node(route, subgraph)
        builder.add_edge(route, "supervisor")

    async def supervisor(state: SupervisorState) -> Command[Any]:
        outcome = await router.decide(state["messages"], handled=dispatched)
        decision = outcome.decision
        target = _resolve_route(decision.next, dispatched=dispatched, tool_counts=tool_counts)
        # 决策事实先落库再派发：派发目标与判定依据在 Run 结束后可复盘（改派前后都在）
        await route_recorder.route_decision(
            decided_route=decision.next,
            dispatched_route=target,
            source=outcome.source,
            reason=decision.reason or None,
        )
        # 路由调用的用量随决策节点写回状态：末次决策（多为 finish）之后没有子图跟进，
        # 若只由子图写回，这一次决策的用量会随 Run 收敛而丢失。
        usage_update: dict[str, Any] = {"token_usage": pipeline.token_usage}
        if target is None:
            return Command(goto=END, update=usage_update)
        dispatched.append(target)
        return Command(goto=target, update=usage_update)

    builder.add_node(
        "supervisor",
        supervisor,
        # Command 的目标在静态图里不可见，显式声明派发去向，拓扑才能被 get_graph/draw_mermaid
        # 完整呈现（子 Agent 节点否则会成孤立点）。
        destinations=(*SUB_AGENT_ROUTES, END),
    )
    builder.add_edge(START, "supervisor")
    return builder.compile(checkpointer=runtime.checkpointer)


def _resolve_route(
    next_route: RouteName,
    *,
    dispatched: Sequence[RouteName],
    tool_counts: Mapping[str, int],
) -> RouteName | None:
    """把路由决策落成可派发目标：收尾、超限或重复派发时返回 None（转 END）。

    授权感知：决策路由当前无可见工具、而另有子 Agent 有工具时，改派首个未派发且有工具的子
    Agent（按 `SUB_AGENT_ROUTES` 优先级）；全部子 Agent 都无工具时按原决策派发，保留无工具
    直答行为。
    """
    if next_route == ROUTE_FINISH or len(dispatched) >= MAX_DISPATCHES:
        return None
    if next_route in dispatched:
        return None
    if tool_counts.get(next_route, 0) == 0 and any(count > 0 for count in tool_counts.values()):
        for route in SUB_AGENT_ROUTES:
            if route not in dispatched and tool_counts.get(route, 0) > 0:
                return route
        return None
    return next_route


__all__ = ["SUB_AGENT_TYPES", "SUPERVISOR_SLUG", "SUPERVISOR_TOOL_IDS", "build_supervisor_graph"]
