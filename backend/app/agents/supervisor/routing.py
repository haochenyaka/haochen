"""Supervisor 路由决策：LLM 结构化输出为主，关键词兜底。

真实模型（`ChatOpenAI`）走结构化输出，让模型在若干子 Agent 间做多轮派发判断；开发态
stub 与测试替身不具备结构化输出能力（且常按调用序列返回，不宜占用），因此只走关键词
兜底——默认落到咨询子 Agent，与接入 supervisor 之前的单 Agent 行为保持一致。

关键词兜底也承担复合请求的拆分：首轮无命中则派发默认子 Agent；已派发过的路由不再重复，
仍在文本里命中未处理的路由（如"订房" + "早餐送到"）时继续派发，直到无可派发目标收尾。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.agents.models import message_text

logger = logging.getLogger(__name__)

RouteName = Literal["consult", "order", "room_service", "handoff", "finish"]

ROUTE_CONSULT: RouteName = "consult"
ROUTE_ORDER: RouteName = "order"
ROUTE_ROOM_SERVICE: RouteName = "room_service"
ROUTE_HANDOFF: RouteName = "handoff"
ROUTE_FINISH: RouteName = "finish"

SUB_AGENT_ROUTES: tuple[RouteName, ...] = (
    ROUTE_CONSULT,
    ROUTE_ORDER,
    ROUTE_ROOM_SERVICE,
    ROUTE_HANDOFF,
)
"""可派发的子 Agent 路由（finish 不是派发目标，单独处理）。"""

DEFAULT_ROUTE: RouteName = ROUTE_CONSULT
"""首轮无明确意图时的默认子 Agent：与既有前台行为一致。"""

MAX_DISPATCHES = 4
"""单次 Run 内最多派发次数：防止 supervisor 与子图互相触发而空转。"""

ROUTING_PROMPT = (
    "你是无人酒店前台的多智能体调度器。根据客人的最新诉求，决定下一步派发给哪个子 Agent：\n"
    "- consult：知识问答、房型与时间等咨询\n"
    "- order：预订、改期、续订、取消、发票等订单事务\n"
    "- room_service：清洁、送物、机器人配送、设施报修等客房服务\n"
    "- handoff：投诉、退款、赔偿、人身安全等需要转人工的诉求\n"
    "- finish：诉求已由已派发的子 Agent 处理完毕，无需再派发\n"
    "已派发过的子 Agent 不再重复派发；一次回复只选一个最关键的下一步。"
)


class RouteDecision(BaseModel):
    """一次路由决策：派发目标与判定依据（依据随审计事实留痕）。"""

    next: RouteName = Field(description="下一步派发的子 Agent；无需再派发时填 finish")
    reason: str = Field(default="", description="一句话说明判定依据")


@dataclass(frozen=True)
class RouteOutcome:
    """决策结果与其来源：source 为 llm（结构化输出）或 keyword（关键词兜底）。"""

    decision: RouteDecision
    source: Literal["llm", "keyword"]


ROUTE_KEYWORDS: tuple[tuple[RouteName, tuple[str, ...]], ...] = (
    # 顺序即优先级：安全/资金类的转人工最先判定，其次客房服务、订单，最后才是泛咨询
    (
        ROUTE_HANDOFF,
        ("投诉", "退款", "赔偿", "转人工", "人工客服", "报警", "遗失", "丢失", "紧急", "安全隐患"),
    ),
    (
        ROUTE_ROOM_SERVICE,
        (
            "打扫",
            "清洁",
            "客房服务",
            "送水",
            "送物",
            "送餐",
            "到房间",
            "毛巾",
            "叫醒",
            "报修",
            "维修",
            "机器人",
            "早餐送到",
        ),
    ),
    (
        ROUTE_ORDER,
        ("预订", "订房", "改期", "续订", "取消订单", "发票", "入住", "退房", "订单", "房费"),
    ),
    (
        ROUTE_CONSULT,
        (
            "房型",
            "价格",
            "多少钱",
            "几点",
            "位置",
            "怎么走",
            "附近",
            "政策",
            "宠物",
            "停车",
            "早餐",
        ),
    ),
)


def matching_routes(text: str) -> tuple[RouteName, ...]:
    """命中关键词的所有路由，按优先级排列（用于复合请求逐个派发）。"""
    return tuple(
        route for route, keywords in ROUTE_KEYWORDS if any(keyword in text for keyword in keywords)
    )


def keyword_route(text: str) -> RouteName | None:
    """按关键词表判定路由：返回优先级最高的路由，无命中返回 None。"""
    matches = matching_routes(text)
    return matches[0] if matches else None


def latest_user_text(messages: Sequence[BaseMessage]) -> str:
    """取最后一条客人消息的文本（路由依据）。"""
    for message in reversed(messages):
        if message.type == "human":
            return message_text(message)
    return ""


class SupervisorRouter:
    """路由决策器：结构化输出优先，失败或不可用时落关键词兜底。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._structured = (
            model.with_structured_output(RouteDecision) if isinstance(model, ChatOpenAI) else None
        )

    async def decide(
        self, messages: Sequence[BaseMessage], *, handled: Sequence[str] = ()
    ) -> RouteOutcome:
        """决定下一步派发目标；handled 为本 Run 已派发过的路由。"""
        if self._structured is not None:
            try:
                decision = await self._structured.ainvoke(
                    [SystemMessage(content=_instruction(handled)), *messages]
                )
            except Exception as exc:  # 结构化输出失败不阻断 Run：落关键词兜底
                logger.warning("supervisor 结构化路由失败，落关键词兜底：%s", exc)
            else:
                if isinstance(decision, RouteDecision):
                    return RouteOutcome(decision=decision, source="llm")
        return RouteOutcome(decision=self._fallback(messages, handled=handled), source="keyword")

    def _fallback(
        self, messages: Sequence[BaseMessage], *, handled: Sequence[str]
    ) -> RouteDecision:
        pending = [
            route for route in matching_routes(latest_user_text(messages)) if route not in handled
        ]
        if pending:
            return RouteDecision(next=pending[0], reason=f"关键词命中路由 {pending[0]}")
        if handled:
            return RouteDecision(next=ROUTE_FINISH, reason="无未处理的路由命中，收尾")
        return RouteDecision(next=DEFAULT_ROUTE, reason="关键词无命中，派发默认子 Agent")


def _instruction(handled: Sequence[str]) -> str:
    if not handled:
        return ROUTING_PROMPT
    return f"{ROUTING_PROMPT}\n\n本次已派发过：{', '.join(handled)}。"


__all__ = [
    "DEFAULT_ROUTE",
    "MAX_DISPATCHES",
    "ROUTE_CONSULT",
    "ROUTE_FINISH",
    "ROUTE_HANDOFF",
    "ROUTE_KEYWORDS",
    "ROUTE_ORDER",
    "ROUTE_ROOM_SERVICE",
    "ROUTING_PROMPT",
    "SUB_AGENT_ROUTES",
    "RouteDecision",
    "RouteName",
    "RouteOutcome",
    "SupervisorRouter",
    "keyword_route",
    "latest_user_text",
    "matching_routes",
]
