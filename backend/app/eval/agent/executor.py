"""真实用例执行器：以生产同源链路跑一条用例，从审计事实回读工具序列。

口径：工具调用序列只从 `tool_audit` 事实回读，与线上可观测口径同源；入参是审计里的
打码副本（`args_digest`）。当前数据集期望参数不含敏感值，打码不影响比对；将来出现含
手机号/证件号的期望参数时需另设旁路。

多轮用例（`turns`）按序在**同一线程**逐轮建消息与请求、逐轮等 Run 收敛；工具序列按轮序
拼接——`seq` 是每个 Run 内独立的序号，跨轮混排会把轮次顺序打乱。因此有状态用例
（下单后改期/取消）可以用第一轮真实建单来制造前置状态，而不是靠历史文本里的假上下文。

任务成功标志取自末轮 Run 终态与该 Run 的工具事实状态：工具以字符串回执业务失败
（"操作失败：…"）时审计状态仍是 completed，此类业务级失败在审计口径下不可见——属已知
边界，由答案评判（P2.3）与回答关键词（`answer_contains`）补偿。

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
from app.eval.agent.dataset import ORDER_NO_PLACEHOLDER, AgentEvalItem
from app.eval.agent.runner import CaseExecution, EvalRunError
from app.eval.agent.seeds import apply_seed, release_eval_orders
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

EVAL_USERNAME_TEMPLATE = "eval-{item_id}"
"""评测客人用户名：夹具与对话入口共用同一派生，保证夹具状态被同一账号看到。"""

RUN_STATUS_COMPLETED = "completed"
"""Run 终态取值（见 `RunOutcome`）：只有 completed 才可能任务成功。"""


def tool_calls_from_audit(entries: Sequence[AuditEntry]) -> list[dict[str, object]]:
    """单次 Run 的审计时间线 → 工具调用序列：只取工具事实，按 seq 升序，入参用打码副本。

    `seq` 在 Run 内唯一，多轮用例须逐轮调用本函数再拼接，不能把多轮事实一起排序。
    """
    tools = [entry for entry in entries if entry.kind == "tool" and entry.tool_name]
    tools.sort(key=lambda entry: entry.seq)
    return [{"name": entry.tool_name, "args": dict(entry.args_digest or {})} for entry in tools]


def task_success_from(outcome: str, entries: Sequence[AuditEntry]) -> bool:
    """任务成功标志：末轮 Run 收敛为 completed 且用例内无工具事实以 failed 收尾。

    业务级失败（工具返回"操作失败：…"字符串）在审计里仍是 completed，此处判为成功，
    属审计口径的已知边界。
    """
    if outcome != RUN_STATUS_COMPLETED:
        return False
    return not any(entry.kind == "tool" and entry.status == TOOL_STATUS_FAILED for entry in entries)


class RealCaseExecutor:
    """真实编译图执行器：接入（线程/消息/请求）→ 队头 Run → 执行体 → 回读审计。

    单轮用例就是只含一条消息的多轮用例；`turns` 里的每条消息在同线程按序各起一个 Run，
    前一轮的业务副作用（订单/任务）对后一轮可见。

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

    async def execute(self, item: AgentEvalItem) -> CaseExecution:
        settings = self._settings or get_settings()
        if not settings.model_api_key:
            raise EvalRunError(
                "模型密钥未配置，真实评测无法执行：工具序列由模型产生，缺密钥只会得到空序列"
            )
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=self._checkpointer,
                resource_policy=self._resource_policy,
                tool_registry=self._tool_registry,
            ),
            session_factory=self._session_factory,
        )
        released = await release_eval_orders(session_factory=self._session_factory)
        if released:
            logger.info("评测用例 %s 开跑前释放旧评测订单 %d 笔", item.id, released)
        order_no = await self._seed_state(item)
        turns = item.effective_turns()
        if order_no is not None:
            turns = tuple(text.replace(ORDER_NO_PLACEHOLDER, order_no) for text in turns)
        tool_calls: list[dict[str, object]] = []
        entries: list[AuditEntry] = []
        thread_id: UUID | None = None
        outcome = ""
        answer: str | None = None
        for turn_no, text in enumerate(turns, start=1):
            thread_id, run_id = await self._intake(item, text=text, thread_id=thread_id)
            outcome = str(await executor.execute(run_id))
            turn_entries = await self._audit_entries(run_id)
            entries.extend(turn_entries)
            tool_calls.extend(tool_calls_from_audit(turn_entries))
            turn_answer = await self._answer_for_run(run_id)
            if turn_answer is not None:
                answer = turn_answer
            logger.info(
                "评测用例 %s 第 %d 轮：run=%s outcome=%s 工具事实=%d 回答=%d 字",
                item.id,
                turn_no,
                run_id,
                outcome,
                len(turn_entries),
                len(turn_answer or ""),
            )
        return CaseExecution(
            tool_calls=tuple(tool_calls),
            task_success=task_success_from(outcome, entries),
            answer=answer,
        )

    async def _intake(
        self, item: AgentEvalItem, *, text: str, thread_id: UUID | None
    ) -> tuple[UUID, UUID]:
        """按线上接入路径建线程与队列；首轮建线程并落历史消息，后续轮复用同一线程。"""
        async with self._session_factory() as session:
            user = await UserRepository(session).get_or_create(
                username=EVAL_USERNAME_TEMPLATE.format(item_id=item.id)
            )
            messages = MessageRepository(session)
            if thread_id is None:
                thread = await ThreadRepository(session).create(
                    user_id=user.id, agent_slug=self._agent_slug
                )
                thread_id = thread.id
                for prior in item.history:
                    await messages.append(
                        thread_id=thread_id, role=prior.role, content={"text": prior.content}
                    )
            user_message = await messages.append(
                thread_id=thread_id, role="user", content={"text": text}
            )
            await RequestRepository(session).create(
                user_id=user.id,
                agent_slug=self._agent_slug,
                thread_id=thread_id,
                message_id=user_message.id,
            )
            plan = await promote_head_if_idle(session, thread_id)
            await session.commit()
        if plan is None:
            raise EvalRunError(f"用例 {item.id} 未能派发 Run：队列头为空")
        return thread_id, plan.run_id

    async def _seed_state(self, item: AgentEvalItem) -> str | None:
        """按用例声明预置前置状态（已支付 / 已入住），返回订单号供占位符替换。

        夹具先行落库再跑对话：后置写路径（续订/发票/入住信息/开门密码）依赖"已支付"等
        无法由多轮对话制造的状态。无声明时返回 None。
        """
        if not item.seed:
            return None
        return await apply_seed(
            session_factory=self._session_factory,
            username=EVAL_USERNAME_TEMPLATE.format(item_id=item.id),
            seed=item.seed,
        )

    async def _audit_entries(self, run_id: UUID) -> list[AuditEntry]:
        async with self._session_factory() as session:
            timeline = await AuditRepository(session).list_timeline(
                run_id=run_id, limit=AUDIT_READ_LIMIT
            )
        return list(timeline.entries)

    async def _answer_for_run(self, run_id: UUID) -> str | None:
        """回读该 Run 的最终助手回复；无回复（失败/中断）返回 None。"""
        async with self._session_factory() as session:
            message = await MessageRepository(session).get_reply_for_run(run_id)
        if message is None or not isinstance(message.content, dict):
            return None
        text = message.content.get("text")
        return text if isinstance(text, str) else None
