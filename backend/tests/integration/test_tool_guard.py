"""ToolGuard 集成验证（真实 PostgreSQL + 完整 Run 链路）。

场景：模型连续两轮点名同参 create_order（不同 tool_call_id）。第一轮真实落单并写
completed 事实；第二轮被 ToolGuard 判定为"已完成同键"，复用既有结果、不产生第二次
副作用，事实记 duration 0 且与首行共享幂等键——同一 Run 内同参副作用只发生一次。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.models.audit import ToolAudit
from app.models.domain import RoomType
from app.repositories import (
    GuestRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class RepeatingToolChatModel(BaseChatModel):
    """前两轮发同参工具调用（不同 id），收到两次工具结果后收尾。"""

    model_name: str = "repeat-tool"
    tool_name: str = "create_order"
    tool_args: dict[str, Any] = Field(default_factory=dict)
    rounds: int = 2

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        results = [message for message in messages if isinstance(message, ToolMessage)]
        if len(results) < self.rounds:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_args),
                        "id": f"call-r{len(results) + 1}",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            reply = AIMessage(
                content="最终回复：" + "；".join(str(message.content) for message in results)
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


def _future(days: int) -> str:
    return (datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)).isoformat()


async def _seed_room_type(session: AsyncSession) -> RoomType:
    room_type = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8].upper()}",
        name="大床房",
        base_price_fen=30000,
        capacity=2,
        bed_type="king",
    )
    await RoomRepository(session).create(
        room_no=f"T{uuid4().hex[:6]}",
        room_type_id=room_type.id,
        floor=3,
        lock_device_no=f"LOCK-{uuid4().hex[:8]}",
    )
    return room_type


def test_repeated_side_effect_call_reuses_result_without_second_order(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    username = f"tool-guard-{uuid4().hex[:8]}"
    model = RepeatingToolChatModel(
        tool_args={
            "room_type_code": "",
            "check_in_date": _future(2),
            "check_out_date": _future(4),
        }
    )
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        async with factory() as session:
            room_type = await _seed_room_type(session)
            await session.commit()
        model.tool_args["room_type_code"] = room_type.code

        executor = RunExecutor(
            AgentRunBody(
                checkpointer=InMemorySaver(),
                resource_policy=StaticResourcePolicy(allowed=frozenset({("tool", "create_order")})),
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            intake = await intake_message(session, username=username, text="帮我订一间大床房")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            user = await UserRepository(session).get_by_username(username)
            assert user is not None
            guest = await GuestRepository(session).get_by_user_id(user.id)
            assert guest is not None
            orders = await OrderRepository(session).list_recent_for_guest(guest.id)
            rows = (
                (
                    await session.execute(
                        select(ToolAudit)
                        .where(ToolAudit.run_id == str(intake.run_id))
                        .order_by(ToolAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "outcome": outcome,
            "orders": len(orders),
            "order_no": orders[0].order_no if orders else None,
            "rows": [
                (row.tool_name, row.status, row.idempotency_key, row.duration_ms) for row in rows
            ],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    # 两轮同参点名：只落一单
    assert values["orders"] == 1
    rows = values["rows"]
    assert [(name, status) for name, status, _, _ in rows] == [
        ("create_order", "completed"),
        ("create_order", "completed"),
    ]
    first_key = rows[0][2]
    assert first_key is not None and first_key.startswith("toolguard:")
    # 第二行是复用行：同幂等键、耗时 0
    assert rows[1][2] == first_key
    assert rows[1][3] == 0
    assert values["order_no"] is not None
