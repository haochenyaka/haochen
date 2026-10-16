"""P1.1 supervisor 拓扑：路由兜底、路由审计与用量、工具声明一致性与注册表发现（不依赖外部服务）。"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI

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
    RouteDecision,
    SupervisorRouter,
    keyword_route,
    matching_routes,
)
from app.agents.toolkits import default_registry
from app.services.audit_service import MODEL_STATUS_COMPLETED, MODEL_STATUS_FAILED

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


class _FakeStructuredChain:
    """结构化输出链替身：返回预置结果或抛出预置异常，记录收到的消息。"""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[list[BaseMessage]] = []

    async def ainvoke(self, messages: list[BaseMessage], config: Any = None) -> Any:
        self.calls.append(list(messages))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _StructuredChatModel(ChatOpenAI):
    """ChatOpenAI 替身：只替换结构化输出链，不发起网络调用。

    路由仅在真 ChatOpenAI 上走结构化输出，故替身必须继承它才能覆盖该分支。
    """

    payload: Any = None
    include_raw: bool = False

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Any:
        self.include_raw = bool(kwargs.get("include_raw"))
        return _FakeStructuredChain(self.payload)


class _RecordingRecorder:
    """审计写入口替身：只记录 model_call 参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def model_call(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _router_with(
    payload: Any,
) -> tuple[SupervisorRouter, _StructuredChatModel, _RecordingRecorder, list[AIMessage]]:
    model = _StructuredChatModel(model="fake-model", api_key="sk-test", payload=payload)
    recorder = _RecordingRecorder()
    seen: list[AIMessage] = []
    router = SupervisorRouter(
        model,
        recorder=recorder,  # type: ignore[arg-type]
        actor_id="router-tester",
        on_usage=seen.append,
    )
    return router, model, recorder, seen


def _usage_reply(prompt_tokens: int = 12, completion_tokens: int = 6) -> AIMessage:
    return AIMessage(
        content="",
        usage_metadata={
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "input_token_details": {"cache_read": 0},
        },
    )


def test_router_audits_structured_decision_and_counts_usage() -> None:
    reply = _usage_reply()
    router, model, recorder, seen = _router_with(
        {
            "raw": reply,
            "parsed": RouteDecision(next=ROUTE_ORDER, reason="订房"),
            "parsing_error": None,
        }
    )

    outcome = asyncio.run(router.decide([HumanMessage(content="我要订房")]))

    assert model.include_raw is True  # 取回原始消息才能记用量，必须带 include_raw
    assert outcome.source == "llm"
    assert outcome.decision.next == ROUTE_ORDER
    assert len(recorder.calls) == 1
    call = recorder.calls[0]
    assert call["model"] == "fake-model"  # model_label 口径
    assert call["status"] == MODEL_STATUS_COMPLETED
    assert call["actor_id"] == "router-tester"
    assert call["prompt_tokens"] == 12
    assert call["completion_tokens"] == 6
    assert seen == [reply]  # 同一次调用的用量并入 Run 总量


def test_router_counts_usage_when_structured_parse_fails() -> None:
    reply = _usage_reply()
    router, _, recorder, seen = _router_with(
        {"raw": reply, "parsed": None, "parsing_error": ValueError("bad json")}
    )

    outcome = asyncio.run(router.decide([HumanMessage(content="你好")]))

    assert outcome.source == "keyword"  # 解析失败落关键词兜底
    assert outcome.decision.next == ROUTE_CONSULT
    assert recorder.calls[0]["status"] == MODEL_STATUS_COMPLETED  # 调用成功，只是解析失败
    assert seen == [reply]  # 已消耗的用量照记


def test_router_records_failure_and_falls_back_without_usage() -> None:
    router, _, recorder, seen = _router_with(RuntimeError("模型不可用"))

    outcome = asyncio.run(router.decide([HumanMessage(content="我要订房")]))

    assert outcome.source == "keyword"  # 调用失败不阻断 Run：落关键词兜底
    assert outcome.decision.next == ROUTE_ORDER
    call = recorder.calls[0]
    assert call["status"] == MODEL_STATUS_FAILED
    assert "RuntimeError" in call["error"]
    assert seen == []  # 失败无用量可计


def test_router_without_audit_wiring_still_routes() -> None:
    model = _StructuredChatModel(
        model="fake-model",
        api_key="sk-test",
        payload={
            "raw": _usage_reply(),
            "parsed": RouteDecision(next=ROUTE_HANDOFF),
            "parsing_error": None,
        },
    )
    router = SupervisorRouter(model)  # 不接线审计与用量：只验路由行为不依赖 recorder

    outcome = asyncio.run(router.decide([HumanMessage(content="我要投诉")]))

    assert outcome.source == "llm"
    assert outcome.decision.next == ROUTE_HANDOFF
