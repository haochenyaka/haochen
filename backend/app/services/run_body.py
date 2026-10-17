"""Agent 运行执行体：数据库历史 → 图装配 → 模型调用 → 消息与审计落库。

消息表是对话事实来源：每次运行以历史整体作为图输入，检查点只做留存与恢复；
模型调用失败在审计中留痕后向上抛，由执行器按失败/取消收敛。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from app.agents import (
    AgentResourceRequest,
    DeclaredBuiltinPolicy,
    ResourcePolicy,
    ResourceRef,
    RuntimeContext,
    ToolRegistry,
    default_registry,
    get_agent,
    get_shared_checkpointer,
    load_prompt_docs,
    load_skill_catalog,
    prepare_agent_runtime_context,
)
from app.config import get_settings
from app.models.runtime import Message
from app.repositories import AgentRunRepository, MessageRepository, ToolApprovalRepository
from app.services.admin_memory_service import DbMemoryProvider
from app.services.agent_config_service import AgentConfigService, AgentRuntimeConfig
from app.services.audit_service import AuditRecorder
from app.services.langfuse_service import get_langfuse
from app.services.run_types import RunInterrupted, RunResult

if TYPE_CHECKING:
    from app.services.run_worker import RunContext

logger = logging.getLogger(__name__)

MESSAGE_FACTORIES = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}


def to_lc_messages(rows: Sequence[Message]) -> list[BaseMessage]:
    """数据库消息 → LangChain 消息；无语义角色的行跳过（不参与对话）。"""
    messages: list[BaseMessage] = []
    for row in rows:
        factory = MESSAGE_FACTORIES.get(row.role)
        text = text_of(row.content)
        if factory is None or not text:
            continue
        messages.append(factory(content=text))
    return messages


def text_of(content: Any) -> str | None:
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    return text if isinstance(text, str) else None


def reply_text(message: BaseMessage) -> str:
    """回复文本：兼容字符串与内容块列表两种返回形态。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return str(content)


def final_reply(messages: Sequence[BaseMessage]) -> AIMessage:
    """最后一条有文本的助手回复；没有则显式失败。

    图可能是路由收尾（未派发任何子 Agent）或子图只留工具消息，末条不一定是助手消息。
    此前直接取 messages[-1] 会把客人原话落成助手回复，既污染对话事实又让答案评判失真。
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage) and reply_text(message).strip():
            return message
    raise RuntimeError("Run 收敛但没有助手回复，拒绝把非助手消息落库")


class AgentRunBody:
    """执行体：检查点与工具注册表可注入（测试用内存实现与自建注册表），默认取进程级实例。"""

    def __init__(
        self,
        *,
        checkpointer: BaseCheckpointSaver | None = None,
        resource_policy: ResourcePolicy | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._resource_policy = resource_policy
        self._tool_registry = tool_registry

    async def __call__(self, ctx: RunContext) -> RunResult | RunInterrupted:
        await ctx.ensure_not_cancelled()
        identity = ctx.identity
        recorder = AuditRecorder(
            run_id=identity.run_id,
            agent_slug=identity.agent_slug,
            session_factory=ctx.session_factory,
        )
        agent = get_agent(identity.agent_slug)

        # Langfuse 可选 trace：模型执行前预创建 trace 并固化 ID
        langfuse_trace_id = await self._prepare_langfuse_trace(ctx, identity)

        async with ctx.session_factory() as session:
            history = await MessageRepository(session).list_for_thread(identity.thread_id)
            agent_config = await AgentConfigService(session).load_runtime_config(
                identity.agent_slug
            )

        resources = await prepare_agent_runtime_context(
            agent_slug=identity.agent_slug,
            user_id=identity.user_id,
            requested=AgentResourceRequest(
                refs=tuple(
                    ResourceRef(kind="tool", id=name)
                    for name in _enabled_tools(agent, agent_config)
                )
                + tuple(
                    ResourceRef(kind="knowledge_base", id=code)
                    for code in _enabled_knowledge_bases(agent, agent_config)
                )
                + tuple(
                    ResourceRef(kind="skill", id=slug)
                    for slug in _enabled_skills(agent, agent_config)
                )
            ),
            policy=self._resource_policy
            or _default_resource_policy(
                declared_tools=frozenset(agent.tool_ids),
                knowledge_bases=frozenset(agent.knowledge_base_ids),
                skills=frozenset(agent.skill_ids),
            ),
        )
        runtime = RuntimeContext(
            agent_slug=identity.agent_slug,
            user_id=identity.user_id,
            thread_id=identity.thread_id,
            run_id=identity.run_id,
            actor_id=ctx.owner_token,
            resources=resources,
            config=agent_config.context,  # 管理端 Agent 配置（config_json.context 段）
            prompt_docs=load_prompt_docs(agent.package_dir()),
            skills=load_skill_catalog(get_settings().skills_root),
            checkpointer=self._checkpointer or await get_shared_checkpointer(),
            recorder=recorder,
            workdir=Path(get_settings().workdir_root) / "threads" / str(identity.thread_id),
            middleware_config=agent_config.middleware,  # 中间件配置段（config_json.middleware）
            tool_registry=self._tool_registry,
            memory_provider=DbMemoryProvider(ctx.session_factory),
            cancel_event=ctx.cancel_event,
            events=ctx.events,
            session_factory=ctx.session_factory,
        )
        graph = await agent.build_graph(runtime)
        resume = await self._resolve_resume(ctx)
        invoke_input: Any = (
            Command(resume=resume) if resume is not None else {"messages": to_lc_messages(history)}
        )
        output = await graph.ainvoke(
            invoke_input,
            config=runtime.thread_config(),
            context=runtime,
        )
        interrupts = output.get("__interrupt__")
        if interrupts:
            # 危险工具挂起等待人工审批：从 `__interrupt__` 落审批事实，Run 收敛为 interrupted
            approvals = _parse_approvals(interrupts)
            await self._persist_approvals(ctx, approvals)
            return RunInterrupted(approvals)

        reply = final_reply(output["messages"])

        await ctx.ensure_not_cancelled()
        async with ctx.session_factory() as session:
            message = await MessageRepository(session).append(
                thread_id=identity.thread_id,
                role="assistant",
                content={"text": reply_text(reply)},
                run_id=str(identity.run_id),
                meta={"agent_slug": identity.agent_slug},
            )
            await session.commit()
            message_id = message.id
        # 事件在消息事实提交之后发射：投影可迟到，事实不可缺失
        await ctx.events.message_completed(text=reply_text(reply), message_id=message_id)
        await self._consume_approval(ctx)

        token_usage = output.get("token_usage")
        return RunResult(
            token_usage=token_usage if isinstance(token_usage, dict) else None,
            langfuse_trace_id=langfuse_trace_id,
        )

    async def _resolve_resume(self, ctx: RunContext) -> dict[str, object] | None:
        """恢复路径：存在已决定（approved/denied）且未消费的审批 → 构造 resume 续跑图。

        不看 Run 状态——resume 时 `claim_resume` 已把状态置回 running；判据是"本 Run 有
        一条已决定未消费的审批"。返回 None 表示首次执行（按历史消息发起）。"""
        async with ctx.session_factory() as session:
            approval = await ToolApprovalRepository(session).get_decided_for_run(
                ctx.identity.run_id
            )
        if approval is None:
            return None
        return {"approved": approval.status == "approved", "reason": approval.reason or ""}

    async def _persist_approvals(self, ctx: RunContext, approvals: list[dict[str, Any]]) -> None:
        async with ctx.session_factory() as session:
            repository = ToolApprovalRepository(session)
            for item in approvals:
                await repository.create_pending(
                    run_id=ctx.identity.run_id,
                    thread_id=ctx.identity.thread_id,
                    call_id=str(item.get("call_id", "")),
                    tool_name=str(item.get("tool_name", "")),
                    reason=item.get("reason"),
                    args_digest=item.get("args_digest"),
                )
            await session.commit()

    async def _consume_approval(self, ctx: RunContext) -> None:
        """resume 后 Run 正常完成：把已决定的审批标记 consumed，避免重复恢复。"""
        async with ctx.session_factory() as session:
            repository = ToolApprovalRepository(session)
            latest = await repository.get_decided_for_run(ctx.identity.run_id)
            if latest is not None:
                await repository.mark_consumed(latest.id)
                await session.commit()

    async def _prepare_langfuse_trace(self, ctx: RunContext, identity: object) -> str | None:
        """Langfuse trace 预创建：独立短事务，失败不阻断 Run。

        从 identity 提取 run_id/thread_id/user_id/agent_slug 后创建 trace，
        将 trace_id 以幂等方式写入 AgentRun.langfuse_trace_id。
        """
        from uuid import uuid4

        run_id = getattr(identity, "run_id", None)
        thread_id = getattr(identity, "thread_id", None)
        user_id = getattr(identity, "user_id", None)
        agent_slug = getattr(identity, "agent_slug", "")

        if not run_id or not thread_id:
            return None

        lf = get_langfuse()
        if not lf.enabled:
            return None

        trace_id = str(uuid4())
        trace_created = lf.create_trace(
            trace_id=trace_id,
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            agent_slug=agent_slug,
        )
        if trace_created is None:
            return None

        # 幂等写入 langfuse_trace_id（仅 NULL 时写）
        try:
            async with ctx.session_factory() as session:
                repo = AgentRunRepository(session)
                await repo.set_langfuse_trace_id(run_id, trace_id)
                await session.commit()
        except Exception as exc:
            logger.warning("langfuse trace_id 落库失败（不阻断 Run）：%s", exc)
        return trace_id


def _enabled_tools(agent: Any, config: AgentRuntimeConfig) -> tuple[str, ...]:
    """工具集合：管理端显式选择优先，未配置（None）时回退 Agent 代码声明。"""
    return config.enabled_tools if config.enabled_tools is not None else agent.tool_ids


def _parse_approvals(interrupts: Sequence[Any]) -> list[dict[str, Any]]:
    """把图的 `__interrupt__` 载荷解析为待审批元数据（ToolApprovalStation 的 interrupt 载荷）。"""
    approvals: list[dict[str, Any]] = []
    for item in interrupts:
        value = getattr(item, "value", None)
        if isinstance(value, dict) and value.get("type") == "tool_approval":
            approvals.append(
                {
                    "call_id": str(value.get("call_id", "")),
                    "tool_name": str(value.get("tool", "")),
                    "reason": value.get("reason"),
                    "args_digest": value.get("args_digest"),
                }
            )
    return approvals


def _enabled_knowledge_bases(agent: Any, config: AgentRuntimeConfig) -> tuple[str, ...]:
    return (
        config.enabled_knowledge_bases
        if config.enabled_knowledge_bases is not None
        else agent.knowledge_base_ids
    )


def _enabled_skills(agent: Any, config: AgentRuntimeConfig) -> tuple[str, ...]:
    return config.enabled_skills if config.enabled_skills is not None else agent.skill_ids


def _default_resource_policy(
    *,
    declared_tools: frozenset[str],
    knowledge_bases: frozenset[str],
    skills: frozenset[str] = frozenset(),
) -> ResourcePolicy:
    """P3 过渡：Agent 代码声明的内置工具、知识库与 Skill 放行（P2 权限数据接管后替换）。

    注册表只放行已注册的声明项：代码声明了但未注册的工具在装配期显式失败（不静默）。
    """
    registered = frozenset(default_registry().names())
    missing = sorted(declared_tools - registered)
    if missing:
        raise LookupError(f"Agent 声明的工具未注册：{', '.join(missing)}")
    return DeclaredBuiltinPolicy(
        declared_tools=declared_tools, knowledge_bases=knowledge_bases, skills=skills
    )
