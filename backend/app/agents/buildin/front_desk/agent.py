"""front-desk 占位图：模型直答 + 工具回环，中间件流水线包裹模型与工具调用（P1）。

模型调用前按工位顺序准备请求（大结果卸载、技能门控、记忆注入、阈值摘要、工具调用
形态修正），调用由 ModelRetry 与 TokenUsage 工位包裹；模型请求的工具调用经 ToolNode
执行（执行处复验门控、写 tool_audit 事实），结果回灌模型后再次作答，直到不再调用工具。
用量与摘要事件随图状态写回，供 Run 终态与检查点引用；面向客户端的事件（message.delta、
tool.call、tool.result）经运行事件发射器投影，事实仍以数据库为准。
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.base import BaseAgent
from app.agents.context import BaseContext, config_item
from app.agents.middlewares import ModelCallRequest, build_middleware_pipeline
from app.agents.models import bind_tools, build_chat_model, invoke_with_audit
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import build_tool_node, default_registry

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


class FrontDeskState(TypedDict):
    """图状态：messages 以数据库历史整体覆盖；用量与摘要事件随 Run 写回检查点。"""

    messages: list[BaseMessage]
    token_usage: dict[str, object] | None
    summarization_events: Annotated[list[dict[str, Any]], operator.add]


class FrontDeskAgent(BaseAgent):
    slug = "front-desk"
    name = "前台助理"
    description = "面向客人的咨询与前台事务：知识咨询、房型与订单查询、预订与订单变更"
    context_schema = FrontDeskContext
    tool_ids = (
        "get_current_time",
        "list_kbs",
        "query_kb",
        "list_room_types",
        "list_my_orders",
        "create_order",
        "reschedule_order",
        "renew_order",
        "cancel_order",
        "apply_invoice",
        "get_checkin_info",
        "deliver_room_password",
    )
    knowledge_base_ids = (HOTEL_KNOWLEDGE_BASE,)
    skill_ids = ("checkin-flow",)

    async def build_graph(
        self, runtime: RuntimeContext
    ) -> CompiledStateGraph[FrontDeskState, RuntimeContext, FrontDeskState, FrontDeskState]:
        context = FrontDeskContext.from_config(runtime.config)
        model = build_chat_model(
            config={"model": context.model, "temperature": context.temperature}
        )
        pipeline = build_middleware_pipeline(
            config=runtime.middleware_config,
            workdir=runtime.workdir,
            thread_id=runtime.thread_id,
            user_id=runtime.user_id,
            run_id=runtime.run_id,
            recorder=runtime.recorder,
            actor_id=runtime.actor_id,
            skills=runtime.skills,
            preload_skills=runtime.resources.ids("skill"),
            memory_provider=runtime.memory_provider,
            steer_session_factory=runtime.session_factory,
            steer_cancel_signal=(
                runtime.cancel_event.set if runtime.cancel_event is not None else None
            ),
            approval_interrupt_enabled=runtime.checkpointer is not None,
        )
        system_prompt = "\n\n".join(
            part
            for part in (context.system_prompt, runtime.prompt_docs, pipeline.skill_prompt)
            if part
        )
        registry = runtime.tool_registry or default_registry()
        tools = registry.select(runtime.resources.ids("tool"))

        async def respond(state: FrontDeskState) -> dict[str, Any]:
            emitted_events = len(pipeline.summarization_events)
            request = await pipeline.prepare_model_call(
                ModelCallRequest(
                    messages=[SystemMessage(content=system_prompt), *state["messages"]],
                    tools=tools,
                )
            )
            visible_model = bind_tools(model, request.tools)
            on_delta = runtime.events.message_delta if runtime.events is not None else None
            reply = await pipeline.invoke_model(
                request,
                base=partial(
                    invoke_with_audit,
                    visible_model,
                    request.messages,
                    recorder=runtime.recorder,
                    actor_id=runtime.actor_id,
                    on_delta=on_delta,
                    retrieval_refs=runtime.retrieval_refs.drain,
                ),
            )
            reply = await pipeline.after_model(request, reply)
            return {
                "messages": [*state["messages"], reply],
                "token_usage": pipeline.token_usage,
                "summarization_events": pipeline.summarization_events[emitted_events:],
            }

        def next_step(state: FrontDeskState) -> str:
            reply = state["messages"][-1]
            return "tools" if getattr(reply, "tool_calls", None) else END

        builder = StateGraph(FrontDeskState, context_schema=RuntimeContext)
        builder.add_node("respond", respond)
        builder.add_edge(START, "respond")
        if tools:
            tool_node = build_tool_node(
                tools=tools,
                pipeline=pipeline,
                recorder=runtime.recorder,
                events=runtime.events,
            )

            async def run_tools(state: FrontDeskState) -> dict[str, Any]:
                output = await tool_node.ainvoke({"messages": state["messages"]})
                return {"messages": [*state["messages"], *output["messages"]]}

            builder.add_node("tools", run_tools)
            builder.add_conditional_edges("respond", next_step, {"tools": "tools", END: END})
            builder.add_edge("tools", "respond")
        else:
            builder.add_edge("respond", END)
        return builder.compile(checkpointer=runtime.checkpointer)
