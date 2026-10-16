"""子 Agent 回环子图工厂：模型直答 + 工具回环，11 道工位统一包裹。

每个子 Agent（含默认咨询子 Agent）都是独立可编译的子图，复用同一份回环结构；supervisor
把子图作为节点嵌入自己的图，子图产出后回到 supervisor 决定是否继续派发。工具可见性由
调用方按授权资源快照裁剪后传入，执行处的门控复验仍由 `build_tool_node` 承担。

系统提示词 = 调用方合成的基础提示词 + 本 Run 已激活 Skill 的正文指令（`pipeline.skill_prompt`
按授权快照装配，无激活 Skill 时为空），二者在装配期合成为唯一一条系统消息。
"""

from __future__ import annotations

import operator
from collections.abc import Sequence
from functools import partial
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langgraph.graph import END, START, StateGraph

from app.agents.middlewares import MiddlewarePipeline, ModelCallRequest
from app.agents.models import bind_tools, invoke_with_audit
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import build_tool_node

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph


class LoopState(TypedDict):
    """回环子图状态：与 supervisor 状态在三个通道上兼容，可直接嵌入。"""

    messages: list[BaseMessage]
    token_usage: dict[str, object] | None
    summarization_events: Annotated[list[dict[str, Any]], operator.add]


def build_loop_subgraph(
    *,
    runtime: RuntimeContext,
    system_prompt: str,
    tools: Sequence[BaseTool],
    model: BaseChatModel,
    pipeline: MiddlewarePipeline,
    agent_slug: str | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """装配一个"模型直答 + 工具回环"子图：无可见工具时直接单轮作答。

    agent_slug 为本子图的审计归属：给出时按它派生记录器，模型与工具事实据此归属到具体子
    Agent；流水线仍为调用方共享的单例，token 用量与工位行为不受影响。
    """
    recorder = runtime.recorder.with_agent(agent_slug) if agent_slug else runtime.recorder
    visible_tools = tuple(tools)
    effective_prompt = "\n\n".join(part for part in (system_prompt, pipeline.skill_prompt) if part)

    async def respond(state: LoopState) -> dict[str, Any]:
        emitted_events = len(pipeline.summarization_events)
        request = await pipeline.prepare_model_call(
            ModelCallRequest(
                messages=[SystemMessage(content=effective_prompt), *state["messages"]],
                tools=visible_tools,
            )
        )
        bound_model = bind_tools(model, request.tools)
        on_delta = runtime.events.message_delta if runtime.events is not None else None
        reply = await pipeline.invoke_model(
            request,
            base=partial(
                invoke_with_audit,
                bound_model,
                request.messages,
                recorder=recorder,
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

    def next_step(state: LoopState) -> str:
        reply = state["messages"][-1]
        return "tools" if getattr(reply, "tool_calls", None) else END

    builder = StateGraph(LoopState, context_schema=RuntimeContext)
    builder.add_node("respond", respond)
    builder.add_edge(START, "respond")
    if visible_tools:
        tool_node = build_tool_node(
            tools=visible_tools,
            pipeline=pipeline,
            recorder=recorder,
            events=runtime.events,
        )

        async def run_tools(state: LoopState) -> dict[str, Any]:
            output = await tool_node.ainvoke({"messages": state["messages"]})
            return {"messages": [*state["messages"], *output["messages"]]}

        builder.add_node("tools", run_tools)
        builder.add_conditional_edges("respond", next_step, {"tools": "tools", END: END})
        builder.add_edge("tools", "respond")
    else:
        builder.add_edge("respond", END)
    return builder.compile()


__all__ = ["LoopState", "build_loop_subgraph"]
