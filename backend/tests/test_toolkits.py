"""工具注册骨架验证：注册元数据、Skill 门控双面（可见/可执行）、ToolNode 装配与审计事实。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, TypedDict, cast
from uuid import UUID, uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from app.agents.middlewares import ModelCallRequest, build_middleware_pipeline
from app.agents.middlewares.filesystem import OFFLOAD_MARKER
from app.agents.middlewares.tool_approval import ApprovalDecision
from app.agents.skills import SkillCatalog, SkillDocument
from app.agents.toolkits import (
    ToolRegistrationError,
    ToolRegistry,
    build_tool_node,
    builtin_tool,
    default_registry,
)
from app.agents.toolkits.registry import BLOCKED_DUPLICATE_REASON
from app.services.audit_service import (
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    AuditRecorder,
)
from app.services.run_events import RunEventEmitter

SKILL_CONFIG = {"skills": {"preload": ["checkin-flow"]}}


def skill_catalog(*slugs: str) -> SkillCatalog:
    """最小 Skill 目录替身：只提供 slug 与渲染用正文，不触磁盘。"""
    return SkillCatalog(
        documents={
            slug: SkillDocument(
                slug=slug,
                name=slug,
                description=f"{slug} 测试用",
                tool_dependencies=(),
                mcp_dependencies=(),
                skill_dependencies=(),
                instructions=f"{slug} 测试说明",
                path=Path("/skills") / slug / "SKILL.md",
            )
            for slug in slugs
        }
    )


class ToolNodeState(TypedDict):
    messages: list[Any]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def invoke_tool_node(node: ToolNode, message: AIMessage) -> dict[str, Any]:
    """在最小图里执行 ToolNode：与 Agent 图同一执行路径（节点外调用缺运行期上下文）。"""
    builder = StateGraph(ToolNodeState)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    result: dict[str, Any] = run(graph.ainvoke({"messages": [message]}))
    return result


class RecordingAudit:
    """审计写入口替身：只记录参数，不触库。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def tool_call(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def audit_spy() -> tuple[AuditRecorder, RecordingAudit]:
    spy = RecordingAudit()
    return cast(AuditRecorder, spy), spy


class RecordingBus:
    """事件总线替身：记录发布的事件（走真实发射器的包络与协议方法）。"""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, run_id: UUID, event: str, data: dict[str, Any]) -> str:
        self.published.append((event, data))
        return f"{len(self.published)}-0"


def event_spy() -> tuple[RunEventEmitter, RecordingBus]:
    bus = RecordingBus()
    emitter = RunEventEmitter(run_id=uuid4(), thread_id=uuid4(), agent_slug="front-desk", bus=bus)
    return emitter, bus


def tool_call_message(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}],
    )


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()

    @builtin_tool(category="room", skill="checkin-flow", display_name="查询房型", registry=registry)
    async def search_room_types(date: str) -> str:
        """按日期查询可订房型。"""
        return f"{date} 可订：大床房"

    @builtin_tool(category="system", registry=registry)
    async def ping(text: str) -> str:
        """回显文本。"""
        return f"pong:{text}"

    return registry


def test_registry_stamps_metadata_and_rejects_duplicates() -> None:
    registry = build_registry()
    assert registry.names() == ("search_room_types", "ping")

    tool = registry.get("search_room_types")
    assert tool.metadata["category"] == "room"
    assert tool.metadata["skill"] == "checkin-flow"
    assert tool.metadata["dangerous"] is False
    assert tool.metadata["read_only"] is True
    assert registry.read_only_names() == ("search_room_types", "ping")

    described = {item["name"]: item for item in registry.describe()}
    assert described["search_room_types"]["display_name"] == "查询房型"
    assert described["search_room_types"]["description"] == "按日期查询可订房型。"
    assert described["search_room_types"]["skill"] == "checkin-flow"
    assert described["ping"]["display_name"] == "ping"

    with pytest.raises(ToolRegistrationError):
        registry.register(tool)
    with pytest.raises(ToolRegistrationError):
        registry.get("missing_tool")

    write_registry = ToolRegistry()

    @builtin_tool(dangerous=True, read_only=False, registry=write_registry)
    async def create_order(room_type: str) -> str:
        """创建订单。"""
        return room_type

    assert isinstance(create_order, BaseTool)
    assert write_registry.read_only_names() == ()
    assert write_registry.describe()[0]["dangerous"] is True


def test_registry_select_follows_authorized_ids() -> None:
    registry = build_registry()
    assert registry.select([]) == ()
    assert [tool.name for tool in registry.select(["ping"])] == ["ping"]
    assert [tool.name for tool in registry.select(["ping", "search_room_types"])] == [
        "ping",
        "search_room_types",
    ]
    with pytest.raises(ToolRegistrationError):
        registry.select(["ping", "not_registered"])


def test_default_registry_ships_read_only_builtin_tools() -> None:
    registry = default_registry()
    assert registry.names() == (
        "query_kb",
        "list_kbs",
        "list_room_types",
        "list_my_orders",
        "create_order",
        "reschedule_order",
        "renew_order",
        "cancel_order",
        "apply_invoice",
        "get_checkin_info",
        "deliver_room_password",
        "request_robot_delivery",
        "request_room_clean",
        "submit_complaint",
        "request_handoff",
        "get_current_time",
    )
    assert registry.read_only_names() == (
        "query_kb",
        "list_kbs",
        "list_room_types",
        "list_my_orders",
        "get_checkin_info",
        "request_robot_delivery",
        "request_room_clean",
        "submit_complaint",
        "get_current_time",
    )
    described = {item["name"]: item for item in registry.describe()}
    assert described["create_order"]["read_only"] is False
    assert described["create_order"]["category"] == "order"
    assert described["request_handoff"]["read_only"] is False
    assert described["request_handoff"]["category"] == "handoff"
    assert described["get_checkin_info"]["read_only"] is True
    assert described["get_checkin_info"]["skill"] == "checkin-flow"
    assert described["get_checkin_info"]["credential_delivery"] is False
    assert described["deliver_room_password"]["read_only"] is False
    assert described["deliver_room_password"]["skill"] == "checkin-flow"
    assert described["deliver_room_password"]["credential_delivery"] is True
    system_tool = described["get_current_time"]
    assert system_tool["read_only"] is True
    assert system_tool["dangerous"] is False
    assert system_tool["category"] == "system"
    result = run(registry.get("get_current_time").ainvoke({}))
    assert "UTC+8" in str(result)


def test_skill_gate_filters_visible_and_blocks_execution() -> None:
    registry = build_registry()
    executed: list[str] = []

    @builtin_tool(skill="checkout-flow", registry=registry)
    async def closed_tool() -> str:
        """未激活 Skill 的工具。"""
        executed.append("closed_tool")
        return "should not run"

    pipeline = build_middleware_pipeline(
        config=SKILL_CONFIG, skills=skill_catalog("checkin-flow", "checkout-flow")
    )
    visible = run(
        pipeline.prepare_model_call(
            ModelCallRequest(
                messages=[],
                tools=registry.select(["ping", "search_room_types", "closed_tool"]),
            )
        )
    )
    # 无 Skill 归属的工具始终可见；未激活 Skill 的工具从模型可见集合中移除
    assert [tool.name for tool in visible.tools] == ["ping", "search_room_types"]
    assert pipeline.allows_tool(registry.get("search_room_types")) is True
    assert pipeline.allows_tool(registry.get("ping")) is True
    assert pipeline.allows_tool(registry.get("closed_tool")) is False

    recorder, spy = audit_spy()
    node = build_tool_node(
        tools=registry.select(["closed_tool"]), pipeline=pipeline, recorder=recorder
    )
    output = invoke_tool_node(node, tool_call_message("closed_tool", {}))
    message = output["messages"][0]
    assert executed == []
    assert isinstance(message, ToolMessage)
    assert message.status == "error"
    assert "Skill 未激活" in str(message.content)
    assert spy.calls[0]["status"] == TOOL_STATUS_FAILED
    assert spy.calls[0]["tool_name"] == "closed_tool"


def test_tool_node_executes_and_records_facts() -> None:
    registry = build_registry()
    pipeline = build_middleware_pipeline(config={})
    recorder, spy = audit_spy()
    node = build_tool_node(
        tools=registry.select(["ping", "search_room_types"]), pipeline=pipeline, recorder=recorder
    )
    output = invoke_tool_node(node, tool_call_message("ping", {"text": "你好"}))
    message = output["messages"][0]
    assert isinstance(message, ToolMessage)
    assert message.content == "pong:你好"
    assert message.tool_call_id == "c1"
    assert message.status == "success"
    assert spy.calls == [
        {
            "tool_name": "ping",
            "status": TOOL_STATUS_COMPLETED,
            "args_digest": {"text": "你好"},
            "idempotency_key": None,
            "duration_ms": spy.calls[0]["duration_ms"],
            "error": None,
        }
    ]
    assert spy.calls[0]["duration_ms"] >= 0


def test_tool_node_emits_call_and_result_events() -> None:
    registry = build_registry()
    pipeline = build_middleware_pipeline(config={})
    recorder, _ = audit_spy()
    emitter, bus = event_spy()
    node = build_tool_node(
        tools=registry.select(["ping"]), pipeline=pipeline, recorder=recorder, events=emitter
    )
    invoke_tool_node(node, tool_call_message("ping", {"text": "你好"}, call_id="c7"))

    assert [event for event, _ in bus.published] == ["tool.call", "tool.result"]
    call_data = bus.published[0][1]
    result_data = bus.published[1][1]
    assert call_data["tool"] == "ping"
    assert call_data["call_id"] == "c7"
    assert call_data["args"] == {"text": "你好"}
    assert result_data["status"] == TOOL_STATUS_COMPLETED
    assert result_data["duration_ms"] >= 0
    assert result_data["error"] is None
    # 包络字段由发射器补齐：同一 Run、序号递增
    assert call_data["run_id"] == result_data["run_id"]
    assert (call_data["seq"], result_data["seq"]) == (1, 2)


def test_tool_node_emits_failed_result_for_tool_error_and_skill_reject() -> None:
    registry = ToolRegistry()

    @builtin_tool(registry=registry)
    async def broken() -> str:
        """总是失败的工具。"""
        raise RuntimeError("下游超时")

    recorder, _ = audit_spy()
    emitter, bus = event_spy()
    node = build_tool_node(
        tools=registry.select(["broken"]),
        pipeline=build_middleware_pipeline(config={}),
        recorder=recorder,
        events=emitter,
    )
    invoke_tool_node(node, tool_call_message("broken", {}))
    failed = bus.published[-1][1]
    assert failed["status"] == TOOL_STATUS_FAILED
    assert "下游超时" in failed["error"]

    reject_registry = ToolRegistry()

    @builtin_tool(skill="checkout-flow", registry=reject_registry)
    async def closed_tool() -> str:
        """未激活 Skill 的工具。"""
        return "should not run"

    reject_emitter, reject_bus = event_spy()
    reject_node = build_tool_node(
        tools=reject_registry.select(["closed_tool"]),
        pipeline=build_middleware_pipeline(
            config=SKILL_CONFIG, skills=skill_catalog("checkin-flow", "checkout-flow")
        ),
        recorder=recorder,
        events=reject_emitter,
    )
    invoke_tool_node(reject_node, tool_call_message("closed_tool", {}))
    assert [event for event, _ in reject_bus.published] == ["tool.call", "tool.result"]
    assert reject_bus.published[1][1]["status"] == TOOL_STATUS_FAILED
    assert "Skill 未激活" in reject_bus.published[1][1]["error"]


def test_tool_node_offloads_large_result_through_filesystem_station(tmp_path: Path) -> None:
    registry = ToolRegistry()

    @builtin_tool(registry=registry)
    async def huge() -> str:
        """返回超长文本。"""
        return "长" * 5000

    pipeline = build_middleware_pipeline(
        config={"filesystem": {"offload_threshold_bytes": 256, "preview_chars": 20}},
        workdir=tmp_path,
    )
    recorder, _ = audit_spy()
    node = build_tool_node(tools=registry.select(["huge"]), pipeline=pipeline, recorder=recorder)
    output = invoke_tool_node(node, tool_call_message("huge", {}))
    content = str(output["messages"][0].content)
    assert content.startswith(OFFLOAD_MARKER)
    assert "outputs" in content
    assert list((tmp_path / "outputs" / "large_tool_results").glob("*.txt"))


def test_tool_node_turns_tool_failure_into_model_visible_error() -> None:
    registry = ToolRegistry()

    @builtin_tool(registry=registry)
    async def broken() -> str:
        """总是失败的工具。"""
        raise RuntimeError("下游超时")

    pipeline = build_middleware_pipeline(config={})
    recorder, spy = audit_spy()
    node = build_tool_node(tools=registry.select(["broken"]), pipeline=pipeline, recorder=recorder)
    output = invoke_tool_node(node, tool_call_message("broken", {}, call_id="c9"))
    message = output["messages"][0]
    assert message.status == "error"
    assert "RuntimeError" in str(message.content)
    assert "下游超时" in str(message.content)
    assert message.tool_call_id == "c9"
    assert spy.calls[0]["status"] == TOOL_STATUS_FAILED
    assert "RuntimeError: 下游超时" == spy.calls[0]["error"]


def test_tool_node_records_unregistered_tool_as_failed_fact() -> None:
    registry = build_registry()
    pipeline = build_middleware_pipeline(config={})
    recorder, spy = audit_spy()
    node = build_tool_node(tools=registry.select(["ping"]), pipeline=pipeline, recorder=recorder)
    output = invoke_tool_node(node, tool_call_message("ghost_tool", {}))
    message = output["messages"][0]
    assert message.status == "error"
    assert spy.calls[0]["tool_name"] == "ghost_tool"
    assert spy.calls[0]["status"] == TOOL_STATUS_FAILED


def test_tool_node_propagates_approval_and_denial() -> None:
    registry = ToolRegistry()

    @builtin_tool(dangerous=True, read_only=False, registry=registry)
    async def rotate_credential() -> str:
        """改房间密码。"""
        return "rotated"

    pipeline = build_middleware_pipeline(config={})
    recorder, spy = audit_spy()
    emitter, bus = event_spy()
    node = build_tool_node(
        tools=registry.select(["rotate_credential"]),
        pipeline=pipeline,
        recorder=recorder,
        events=emitter,
    )
    from app.agents.middlewares import ToolApprovalRequired, ToolDeniedError

    with pytest.raises(ToolApprovalRequired):
        invoke_tool_node(node, tool_call_message("rotate_credential", {}))
    assert spy.calls == []
    # 审批等待：发射 interrupt 事件位后向上抛出；工具未执行、无 tool.result
    assert [event for event, _ in bus.published] == ["tool.call", "interrupt"]
    assert bus.published[1][1]["tool"] == "rotate_credential"
    assert "审批" in bus.published[1][1]["reason"]

    class DenyPolicy:
        def decide(self, call: Any) -> ApprovalDecision:
            return ApprovalDecision("deny", "测试拒绝")

    denied_pipeline = build_middleware_pipeline(config={}, approval_policy=DenyPolicy())
    denied_recorder, denied_spy = audit_spy()
    denied_emitter, denied_bus = event_spy()
    denied_node = build_tool_node(
        tools=registry.select(["rotate_credential"]),
        pipeline=denied_pipeline,
        recorder=denied_recorder,
        events=denied_emitter,
    )
    with pytest.raises(ToolDeniedError):
        invoke_tool_node(denied_node, tool_call_message("rotate_credential", {}))
    assert denied_spy.calls == []
    # 拒绝是策略结论：只有调用投影，没有结果事实与结果事件
    assert [event for event, _ in denied_bus.published] == ["tool.call"]


def test_unknown_registry_id_fails_explicitly() -> None:
    registry = build_registry()
    with pytest.raises(ToolRegistrationError):
        registry.get("create_order")
    with pytest.raises(ToolRegistrationError):
        registry.select(["create_order"])


def build_order_registry(executed: list[str], *, delay: float = 0.0) -> ToolRegistry:
    registry = ToolRegistry()

    @builtin_tool(read_only=False, category="order", registry=registry)
    async def create_order(room_type: str) -> str:
        """创建订单。"""
        if delay:
            await asyncio.sleep(delay)
        executed.append(room_type)
        return f"已创建 {room_type} 订单"

    return registry


def test_tool_guard_reuses_completed_side_effect_result() -> None:
    executed: list[str] = []
    registry = build_order_registry(executed)
    pipeline = build_middleware_pipeline(config={}, run_id=uuid4())
    recorder, spy = audit_spy()
    emitter, bus = event_spy()
    node = build_tool_node(
        tools=registry.select(["create_order"]),
        pipeline=pipeline,
        recorder=recorder,
        events=emitter,
    )
    first = invoke_tool_node(
        node, tool_call_message("create_order", {"room_type": "大床房"}, call_id="c1")
    )
    assert first["messages"][0].content == "已创建 大床房 订单"

    second = invoke_tool_node(
        node, tool_call_message("create_order", {"room_type": "大床房"}, call_id="c2")
    )
    reused = second["messages"][0]
    assert isinstance(reused, ToolMessage)
    assert reused.content == "已创建 大床房 订单"
    assert reused.tool_call_id == "c2"
    assert reused.status == "success"
    assert executed == ["大床房"]

    keys = [call["idempotency_key"] for call in spy.calls]
    assert keys[0] is not None and keys[0] == keys[1]
    assert spy.calls[0]["status"] == TOOL_STATUS_COMPLETED
    assert spy.calls[1]["status"] == TOOL_STATUS_COMPLETED
    assert spy.calls[1]["duration_ms"] == 0
    assert spy.calls[1]["args_digest"] == {"room_type": "大床房"}
    assert [event for event, _ in bus.published] == [
        "tool.call",
        "tool.result",
        "tool.call",
        "tool.result",
    ]
    assert bus.published[3][1]["duration_ms"] == 0
    assert pipeline.tool_guard_events == [
        {"outcome": "reused", "tool": "create_order", "key": keys[0]}
    ]


def test_tool_guard_blocks_concurrent_duplicate_side_effect_call() -> None:
    executed: list[str] = []
    registry = build_order_registry(executed, delay=0.05)
    pipeline = build_middleware_pipeline(config={}, run_id=uuid4())
    recorder, spy = audit_spy()
    emitter, bus = event_spy()
    node = build_tool_node(
        tools=registry.select(["create_order"]),
        pipeline=pipeline,
        recorder=recorder,
        events=emitter,
    )
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_order",
                "args": {"room_type": "大床房"},
                "id": "c1",
                "type": "tool_call",
            },
            {
                "name": "create_order",
                "args": {"room_type": "大床房"},
                "id": "c2",
                "type": "tool_call",
            },
        ],
    )
    output = invoke_tool_node(node, message)
    by_id = {str(item.tool_call_id): item for item in output["messages"]}
    assert by_id["c1"].status == "success"
    assert by_id["c1"].content == "已创建 大床房 订单"
    assert by_id["c2"].status == "error"
    assert BLOCKED_DUPLICATE_REASON in str(by_id["c2"].content)
    assert executed == ["大床房"]

    # 事实按完成时序落库：被拦一行与成功一行共享幂等键
    completed = [call for call in spy.calls if call["status"] == TOOL_STATUS_COMPLETED]
    blocked = [call for call in spy.calls if call["status"] == TOOL_STATUS_FAILED]
    assert len(completed) == 1 and len(blocked) == 1
    assert blocked[0]["error"] == BLOCKED_DUPLICATE_REASON
    keys = {call["idempotency_key"] for call in spy.calls}
    assert None not in keys and len(keys) == 1
    key = completed[0]["idempotency_key"]
    results = sorted(item["status"] for event, item in bus.published if event == "tool.result")
    assert results == [TOOL_STATUS_COMPLETED, TOOL_STATUS_FAILED]
    assert [event for event, _ in bus.published].count("tool.call") == 2
    assert pipeline.tool_guard_events == [
        {"outcome": "blocked", "tool": "create_order", "key": key}
    ]


def test_tool_guard_leaves_read_only_tools_unguarded() -> None:
    registry = build_registry()
    pipeline = build_middleware_pipeline(config={}, run_id=uuid4())
    recorder, spy = audit_spy()
    node = build_tool_node(tools=registry.select(["ping"]), pipeline=pipeline, recorder=recorder)
    invoke_tool_node(node, tool_call_message("ping", {"text": "你好"}, call_id="c1"))
    invoke_tool_node(node, tool_call_message("ping", {"text": "你好"}, call_id="c2"))
    assert [call["idempotency_key"] for call in spy.calls] == [None, None]
    assert pipeline.tool_guard_events == []


def test_tool_guard_releases_key_after_tool_failure_allowing_retry() -> None:
    attempts: list[str] = []
    registry = ToolRegistry()

    @builtin_tool(read_only=False, registry=registry)
    async def create_order(room_type: str) -> str:
        """第一次失败、第二次成功的下单工具。"""
        attempts.append(room_type)
        if len(attempts) == 1:
            raise RuntimeError("库存服务抖动")
        return f"已创建 {room_type} 订单"

    pipeline = build_middleware_pipeline(config={}, run_id=uuid4())
    recorder, spy = audit_spy()
    node = build_tool_node(
        tools=registry.select(["create_order"]), pipeline=pipeline, recorder=recorder
    )
    failed = invoke_tool_node(
        node, tool_call_message("create_order", {"room_type": "大床房"}, call_id="c1")
    )
    assert failed["messages"][0].status == "error"
    retried = invoke_tool_node(
        node, tool_call_message("create_order", {"room_type": "大床房"}, call_id="c2")
    )
    assert retried["messages"][0].status == "success"
    assert attempts == ["大床房", "大床房"]
    assert spy.calls[0]["status"] == TOOL_STATUS_FAILED
    assert spy.calls[1]["status"] == TOOL_STATUS_COMPLETED
    assert spy.calls[1]["error"] is None
    # 失败行与重试行共享幂等键：失败后释放、允许重试的证据
    assert spy.calls[0]["idempotency_key"] is not None
    assert spy.calls[0]["idempotency_key"] == spy.calls[1]["idempotency_key"]
    assert pipeline.tool_guard_events == []
