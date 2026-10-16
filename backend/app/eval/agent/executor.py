"""真实用例执行器：以生产同源链路跑一条用例，从审计事实回读工具序列。

口径：工具调用序列只从 `tool_audit` 事实回读，与线上可观测口径同源；入参是审计里的
打码副本（`args_digest`）。当前数据集期望参数不含敏感值，打码不影响比对；将来出现含
手机号/证件号的期望参数时需另设旁路。

任务成功标志取自 Run 终态与工具事实状态：工具以字符串回执业务失败（"操作失败：…"）时
审计状态仍是 completed，此类业务级失败在审计口径下不可见——属已知边界，由答案评判
（P2.3）与回答关键词（`answer_contains`）补偿。

无模型密钥时显式失败：工具序列由模型产生，缺密钥只会得到空序列（分数恒 0）而不报错，
那比失败更糟。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import ResourcePolicy, ToolRegistry
from app.config import Settings, get_settings
from app.db import SessionFactory
from app.eval.agent.dataset import AgentEvalItem
from app.eval.agent.runner import EvalRunError
from app.repositories import (
    AUDIT_READ_LIMIT,
    AuditRepository,
    MessageRepository,
    RequestRepository,
    ThreadRepository,
    UserRepository,
)
from app.services.audit_service import TOOL_STATUS_FAILED
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from app.services.thread_dispatch import promote_head_if_idle

if TYPE_CHECKING:
    from langgraph.checkpoint.base import BaseCheckpointSaver

    from app.repositories.audit_repository import AuditEntry

logger = logging.getLogger(__name__)

DEFAULT_EVAL_AGENT_SLUG = "front-desk"
"""评估对话的入口 slug：与线上客人对话同入口，评测口径与线上不漂移。"""

RUN_STATUS_COMPLETED = "completed"
"""Run 终态取值（见 `RunOutcome`）：只有 completed 才可能任务成功。"""


def tool_calls_from_audit(entries: Sequence[AuditEntry]) -> list[dict[str, object]]:
    """审计时间线 → 工具调用序列：只取工具事实，按 seq 升序，入参用打码副本。"""
    tools = [entry for entry in entries if entry.kind == "tool" and entry.tool_name]
    tools.sort(key=lambda entry: entry.seq)
    return [{"name": entry.tool_name, "args": dict(entry.args_digest or {})} for entry in tools]


def task_success_from(outcome: str, entries: Sequence[AuditEntry]) -> bool:
    """任务成功标志：Run 收敛为 completed 且无工具事实以 failed 收尾。

    业务级失败（工具返回"操作失败：…"字符串）在审计里仍是 completed，此处判为成功，
    属审计口径的已知边界。
    """
    if outcome != RUN_STATUS_COMPLETED:
        return False
    return not any(entry.kind == "tool" and entry.status == TOOL_STATUS_FAILED for entry in entries)


class RealCaseExecutor:
    """真实编译图执行器：接入（线程/消息/请求）→ 队头 Run → 执行体 → 回读审计。

    检查点、资源策略与工具注册表可注入（测试用内存检查点与自建注册表）；缺省走进程级
    检查点与 Agent 代码声明的放行策略，与 `AgentRunBody` 一致。
    """

    name = "real-graph"

    def __init__(
        self,
        *,
        agent_slug: str = DEFAULT_EVAL_AGENT_SLUG,
        checkpointer: BaseCheckpointSaver | None = None,
        resource_policy: ResourcePolicy | None = None,
        tool_registry: ToolRegistry | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        settings: Settings | None = None,
    ) -> None:
        self._agent_slug = agent_slug
        self._checkpointer = checkpointer
        self._resource_policy = resource_policy
        self._tool_registry = tool_registry
        self._session_factory = session_factory
        self._settings = settings

    async def execute(self, item: AgentEvalItem) -> tuple[list[dict[str, object]], bool | None]:
        settings = self._settings or get_settings()
        if not settings.model_api_key:
            raise EvalRunError(
                "模型密钥未配置，真实评测无法执行：工具序列由模型产生，缺密钥只会得到空序列"
            )
        run_id = await self._intake(item)
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=self._checkpointer,
                resource_policy=self._resource_policy,
                tool_registry=self._tool_registry,
            ),
            session_factory=self._session_factory,
        )
        outcome = await executor.execute(run_id)
        entries = await self._audit_entries(run_id)
        logger.info(
            "评测用例 %s：run=%s outcome=%s 工具事实=%d", item.id, run_id, outcome, len(entries)
        )
        return tool_calls_from_audit(entries), task_success_from(str(outcome), entries)

    async def _intake(self, item: AgentEvalItem) -> UUID:
        """按线上接入路径建线程与队列：每条用例独立线程，历史消息先落库再发本轮消息。"""
        async with self._session_factory() as session:
            user = await UserRepository(session).get_or_create(username=f"eval-{item.id}")
            thread = await ThreadRepository(session).create(
                user_id=user.id, agent_slug=self._agent_slug
            )
            messages = MessageRepository(session)
            for prior in item.history:
                await messages.append(
                    thread_id=thread.id, role=prior.role, content={"text": prior.content}
                )
            user_message = await messages.append(
                thread_id=thread.id, role="user", content={"text": item.message}
            )
            await RequestRepository(session).create(
                user_id=user.id,
                agent_slug=self._agent_slug,
                thread_id=thread.id,
                message_id=user_message.id,
            )
            plan = await promote_head_if_idle(session, thread.id)
            await session.commit()
        if plan is None:
            raise EvalRunError(f"用例 {item.id} 未能派发 Run：队列头为空")
        return plan.run_id

    async def _audit_entries(self, run_id: UUID) -> list[AuditEntry]:
        async with self._session_factory() as session:
            timeline = await AuditRepository(session).list_timeline(
                run_id=run_id, limit=AUDIT_READ_LIMIT
            )
        return list(timeline.entries)
