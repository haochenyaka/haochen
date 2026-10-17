"""Agent 装配共用件：中间件流水线构造 + 回环子 Agent 基类。

`build_agent_pipeline` 让 supervisor 与各子 Agent 用同一份配置装配 11 道工位（凭证打码、
tool_guard 幂等、token_usage 全树一致）；`LoopSubAgent` 承担子 Agent 的通用装配：可见工具
按授权快照裁剪、系统提示词由"默认提示词 + AGENTS.md"合成，运行入口与 supervisor 内嵌
两条路径共用同一实现。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.agents.base import BaseAgent
from app.agents.middlewares import build_middleware_pipeline
from app.agents.models import build_chat_model
from app.agents.prompts import load_prompt_docs, runtime_context_prompt
from app.agents.runtime import RuntimeContext
from app.agents.supervisor.contexts import SubAgentContext
from app.agents.supervisor.subgraph import build_loop_subgraph
from app.agents.toolkits import default_registry

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.graph.state import CompiledStateGraph

    from app.agents.middlewares import MiddlewarePipeline


def build_agent_pipeline(runtime: RuntimeContext) -> MiddlewarePipeline:
    """按运行期上下文装配中间件流水线：supervisor 与子 Agent 共用。"""
    return build_middleware_pipeline(
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


class LoopSubAgent(BaseAgent):
    """回环子 Agent 基类：声明 slug/工具/提示词，装配逻辑集中在这里。"""

    context_schema: type[SubAgentContext] = SubAgentContext

    def visible_tools(
        self, runtime: RuntimeContext, extra_ids: Sequence[str] = ()
    ) -> tuple[BaseTool, ...]:
        """按授权快照裁剪本 Agent 声明的工具；extra_ids 由 supervisor 追加（如未分类工具）。"""
        available = runtime.resources.ids("tool")
        names = tuple(name for name in self.tool_ids if name in available) + tuple(extra_ids)
        registry = runtime.tool_registry or default_registry()
        return registry.select(names)

    def compose_system_prompt(self, override: str | None = None) -> str:
        """系统提示词 = 覆盖值（或默认提示词）+ 本子包 AGENTS.md + 运行期上下文（当天日期）。"""
        parts = (
            override or self.default_system_prompt,
            load_prompt_docs(self.package_dir()),
            runtime_context_prompt(),
        )
        return "\n\n".join(part for part in parts if part)

    def build_subgraph(
        self,
        runtime: RuntimeContext,
        *,
        model: BaseChatModel,
        pipeline: MiddlewarePipeline,
        system_prompt: str,
        tools: Sequence[BaseTool],
    ) -> CompiledStateGraph[Any, Any, Any, Any]:
        return build_loop_subgraph(
            runtime=runtime,
            system_prompt=system_prompt,
            tools=tools,
            model=model,
            pipeline=pipeline,
            agent_slug=self.slug,
        )

    async def build_graph(self, runtime: RuntimeContext) -> CompiledStateGraph[Any, Any, Any, Any]:
        """独立运行入口：按本 Agent 配置装配模型与流水线后编译子图。"""
        context = SubAgentContext.from_config(runtime.config)
        model = build_chat_model(
            config={"model": context.model, "temperature": context.temperature}
        )
        pipeline = build_agent_pipeline(runtime)
        return self.build_subgraph(
            runtime,
            model=model,
            pipeline=pipeline,
            system_prompt=self.compose_system_prompt(context.system_prompt or None),
            tools=self.visible_tools(runtime),
        )


__all__ = ["LoopSubAgent", "build_agent_pipeline"]
