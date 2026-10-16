"""P1.1 supervisor 拓扑：路由兜底、工具声明一致性与注册表发现（不依赖外部服务）。"""

from __future__ import annotations

import asyncio

from langchain_core.messages import HumanMessage

from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.models import DevStubChatModel
from app.agents.registry import discover_agents
from app.agents.supervisor.graph import SUB_AGENT_TYPES, SUPERVISOR_TOOL_IDS
from app.agents.supervisor.routing import (
    MAX_DISPATCHES,
    ROUTE_CONSULT,
    ROUTE_FINISH,
    ROUTE_HANDOFF,
    ROUTE_ORDER,
    ROUTE_ROOM_SERVICE,
    SupervisorRouter,
    keyword_route,
    matching_routes,
)
from app.agents.toolkits import default_registry

SUB_AGENT_SLUGS = {
    ROUTE_CONSULT: "front-desk-consult",
    ROUTE_ORDER: "front-desk-order",
    ROUTE_ROOM_SERVICE: "room-service",
    ROUTE_HANDOFF: "handoff",
}


def test_supervisor_tool_union_matches_sub_agent_declarations() -> None:
    union = tuple(
        dict.fromkeys(
            tool_id for agent_type in SUB_AGENT_TYPES.values() for tool_id in agent_type.tool_ids
        )
    )
    assert SUPERVISOR_TOOL_IDS == union
    assert FrontDeskAgent.tool_ids == union


def test_sub_agent_declared_tools_are_registered() -> None:
    registered = set(default_registry().names())
    for agent_type in SUB_AGENT_TYPES.values():
        missing = set(agent_type.tool_ids) - registered
        assert missing == set(), f"{agent_type.slug} 声明了未注册的工具：{missing}"


def test_sub_agents_are_discovered_by_registry() -> None:
    agents = discover_agents(refresh=True)
    for slug in SUB_AGENT_SLUGS.values():
        assert slug in agents


def test_keyword_route_priority_is_handoff_first() -> None:
    assert keyword_route("我要投诉，还要退款") == ROUTE_HANDOFF
    assert keyword_route("帮我打扫一下房间") == ROUTE_ROOM_SERVICE
    assert keyword_route("我想预订明晚的房间") == ROUTE_ORDER
    assert keyword_route("大床房多少钱") == ROUTE_CONSULT
    assert keyword_route("你好") is None


def test_matching_routes_returns_all_hits_in_priority_order() -> None:
    routes = matching_routes("我要订房，顺便叫一份早餐到房间")
    assert routes[0] == ROUTE_ROOM_SERVICE
    assert ROUTE_ORDER in routes


def _dispatch_sequence(text: str) -> list[str]:
    """用 stub 模型（无结构化输出能力）模拟 supervisor 兜底路由的连续派发。"""

    async def run() -> list[str]:
        router = SupervisorRouter(DevStubChatModel())
        handled: list[str] = []
        for _ in range(MAX_DISPATCHES):
            outcome = await router.decide([HumanMessage(content=text)], handled=handled)
            assert outcome.source == "keyword"  # stub 无结构化输出能力，只走关键词兜底
            if outcome.decision.next == ROUTE_FINISH:
                break
            handled.append(outcome.decision.next)
        return handled

    return asyncio.run(run())


def test_composite_request_is_split_across_order_and_service() -> None:
    dispatched = _dispatch_sequence("我要订房，顺便叫一份早餐到房间")
    assert {ROUTE_ORDER, ROUTE_ROOM_SERVICE} <= set(dispatched)


def test_plain_request_falls_back_to_consult_then_finishes() -> None:
    assert _dispatch_sequence("你好") == [ROUTE_CONSULT]


def test_no_route_is_dispatched_twice() -> None:
    dispatched = _dispatch_sequence("我要投诉")
    assert dispatched == [ROUTE_HANDOFF]
