"""入住工具集成测试（真实 PostgreSQL + 门锁替身 + 完整 Run 链路）。

覆盖：入住信息只读视图（未就绪 / 就绪）；下发工具按下发即换码（旧行 revoked、新行
active、订单事件 credential_delivered）；归属与状态复验（他人订单按不存在口径、未支付
拒绝）；完整 Run 的明文边界——模型回复增量可含明文，消息落库、检查点与事件投影一律
打码（A2 明文零落库）。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.context import ResourceSnapshot
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import default_registry
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.models.audit import ToolAudit
from app.models.domain import Guest, LockCredential, Order, Room, RoomType
from app.repositories import (
    GuestRepository,
    MessageRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.services.audit_service import AuditRecorder
from app.services.order_service import OrderService
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, RecordingRunEventBus, intake_message
from tests.integration.test_lock_credentials import FakeLockClient, _StaticLockClient

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

NOT_FOUND_TEXT = "没有找到对应的订单，请核对订单号后重试。"
NOT_READY_TEXT = "入住凭证尚未就绪，请稍后重试或联系前台。"
DELIVER_NOTICE_PATTERN = re.compile(r"下发的开门密码：(\S+)")


def _future(days: int) -> date:
    return datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)


async def _call(
    tool_name: str,
    context: RuntimeContext | None,
    *,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> str:
    tool = default_registry().get(tool_name)
    runtime = SimpleNamespace(context=context, tool_call_id=tool_call_id)
    return str(await tool.coroutine(runtime=runtime, **kwargs))


async def _guest(session: AsyncSession, *, username: str | None = None) -> Guest:
    user = await UserRepository(session).get_or_create(
        username=username or f"checkin-{uuid4().hex[:8]}"
    )
    return await GuestRepository(session).get_or_create_by_user(user.id)


async def _seed_paid_order(session: AsyncSession, *, guest_id: UUID) -> tuple[Order, Room]:
    room_type: RoomType = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8].upper()}",
        name="大床房",
        base_price_fen=39800,
        capacity=2,
        bed_type="king",
    )
    room: Room = await RoomRepository(session).create(
        room_no=f"C{uuid4().hex[:6].upper()}",
        room_type_id=room_type.id,
        floor=5,
        lock_device_no=f"LOCK-{uuid4().hex[:8]}",
    )
    order = await OrderRepository(session).create(
        order_no=f"H{uuid4().hex[:10].upper()}",
        guest_id=guest_id,
        room_type_id=room_type.id,
        check_in_date=_future(1),
        check_out_date=_future(3),
        nights=2,
        amount_fen=79600,
    )
    service = OrderService(session)
    await service.transition(order.id, "pending_payment")
    order = await service.transition(order.id, "paid")
    return order, room


def _runtime_context(factory: SessionFactoryArg, *, user_id: UUID) -> RuntimeContext:
    return RuntimeContext(
        agent_slug="front-desk",
        user_id=user_id,
        thread_id=uuid4(),
        run_id=uuid4(),
        actor_id="checkin-tool-test",
        resources=ResourceSnapshot(),
        config={},
        prompt_docs="",
        checkpointer=None,
        recorder=AuditRecorder(run_id=uuid4(), agent_slug="front-desk", session_factory=factory),
        session_factory=factory,
    )


async def _credential_rows(session: AsyncSession, *, room_no: str) -> list[LockCredential]:
    stmt = (
        select(LockCredential)
        .where(LockCredential.room_no == room_no)
        .order_by(LockCredential.version.asc())
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars())


def test_get_checkin_info_reports_room_and_window(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLockClient()
    monkeypatch.setattr("app.agents.toolkits.checkin.LockClient", _StaticLockClient(fake))

    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        async with factory() as session:
            guest = await _guest(session)
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            user_id, order_no, room_no = guest.user_id, order.order_no, room.room_no
            context = _runtime_context(factory, user_id=user_id)

            before = await _call("get_checkin_info", context, order_no=order_no)
            client = fake
            from app.lock.service import LockCredentialService

            await LockCredentialService(session).issue_for_order(order_id=order.id, client=client)
            await session.commit()
            after = await _call("get_checkin_info", context, order_no=order_no)
            foreign = await _call("get_checkin_info", context, order_no="H0000000000")
        return {"before": before, "after": after, "foreign": foreign, "room_no": room_no}

    values = run_with_session_factory(runtime_db_url, _body)
    assert "尚未就绪" in values["before"]
    assert values["room_no"] in values["after"]
    assert "楼层：5 层" in values["after"]
    assert "门锁凭证有效期" in values["after"]
    assert "[已打码]" not in values["after"]  # 只读视图不含密码字段
    assert values["foreign"] == NOT_FOUND_TEXT


def test_deliver_room_password_rotates_and_reports(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLockClient()
    monkeypatch.setattr("app.agents.toolkits.checkin.LockClient", _StaticLockClient(fake))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            other = await _guest(session)
            await session.commit()
            order_no, room_no = order.order_no, room.room_no
            user_id, other_user_id = guest.user_id, other.user_id

            from app.lock.service import LockCredentialService

            issued = await LockCredentialService(session).issue_for_order(
                order_id=order.id, client=fake
            )
            await session.commit()

            context = _runtime_context(factory, user_id=user_id)
            text = await _call("deliver_room_password", context, order_no=order_no)

            foreign_context = _runtime_context(factory, user_id=other_user_id)
            foreign = await _call("deliver_room_password", foreign_context, order_no=order_no)

            reloaded = await OrderRepository(session).get_by_id(order.id)
            assert reloaded is not None
            rows = [
                (row.version, row.status, row.reason)
                for row in await _credential_rows(session, room_no=room_no)
            ]
        return {
            "text": text,
            "foreign": foreign,
            "rows": rows,
            "issued_code": issued.code,
            "device_code": fake.codes[room_no],
            "events": [
                event for event in reloaded.events if event.get("type") == "credential_delivered"
            ],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["text"].startswith("开门密码已重新生成：密码：")
    assert values["device_code"] in values["text"]
    assert values["device_code"] != values["issued_code"]
    assert values["rows"] == [(1, "revoked", "checkin"), (2, "active", "delivery")]
    assert values["foreign"] == NOT_FOUND_TEXT
    assert len(values["events"]) == 1
    assert values["events"][0]["version"] == 2


def test_deliver_rejects_unpaid_order(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            order, _ = await _seed_paid_order(session, guest_id=guest.id)
            unpaid = await OrderRepository(session).create(
                order_no=f"H{uuid4().hex[:10].upper()}",
                guest_id=guest.id,
                room_type_id=order.room_type_id,
                check_in_date=order.check_in_date,
                check_out_date=order.check_out_date,
                nights=2,
                amount_fen=79600,
            )
            await session.commit()
            context = _runtime_context(factory, user_id=guest.user_id)
            return {"text": await _call("deliver_room_password", context, order_no=unpaid.order_no)}

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["text"].startswith("操作失败：")
    assert "不能下发开门密码" in values["text"]


class DeliveryEchoModel(BaseChatModel):
    """先点名下发工具；收到工具结果后按请求里的下发提示复述密码（模拟照提示告知客人）。"""

    model_name: str = "delivery-echo"
    tool_name: str = "deliver_room_password"
    tool_args: dict[str, Any] = Field(default_factory=dict)
    notices: list[str] = Field(default_factory=list)

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
        if not results:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_args),
                        "id": "call-deliver-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            head = messages[0].content if isinstance(messages[0], SystemMessage) else ""
            self.notices.append(str(head))
            match = DELIVER_NOTICE_PATTERN.search(str(head))
            code = match.group(1) if match else "（未收到提示）"
            reply = AIMessage(content=f"您的开门密码是 {code}，旧密码已失效，请及时保存。")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


def test_delivery_plaintext_never_reaches_persistence(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """明文边界：实时增量可含明文；消息落库、检查点、事件投影与工具事实全部打码。"""
    from langgraph.checkpoint.memory import InMemorySaver

    fake = FakeLockClient()
    model = DeliveryEchoModel()
    monkeypatch.setattr("app.agents.toolkits.checkin.LockClient", _StaticLockClient(fake))
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        checkpointer = InMemorySaver()
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(checkpointer=checkpointer),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        username = f"delivery-{uuid4().hex[:8]}"
        async with factory() as session:
            guest = await _guest(session, username=username)
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_no, room_no = order.order_no, room.room_no

            from app.lock.service import LockCredentialService

            await LockCredentialService(session).issue_for_order(order_id=order.id, client=fake)
            await session.commit()

            # 模型在运行时才知道订单号：工具入参先留空，播种后回填
            model.tool_args["order_no"] = order_no
            intake = await intake_message(session, username=username, text="我要开门密码")
            assert intake.run_id is not None
            outcome = await executor.execute(intake.run_id)

            reloaded = await OrderRepository(session).get_by_id(order.id)
            assert reloaded is not None
            rows = [
                (row.version, row.status, row.reason)
                for row in await _credential_rows(session, room_no=room_no)
            ]
            messages = await MessageRepository(session).list_for_thread(intake.thread_id)
            audit_rows = (
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
            "run_id": intake.run_id,
            "thread_id": intake.thread_id,
            "order_no": order_no,
            "events": bus.records(intake.run_id),
            "messages": [(row.role, str(row.content.get("text"))) for row in messages],
            "audit": [
                (row.tool_name, row.status, row.args_digest, row.idempotency_key)
                for row in audit_rows
            ],
            "rows": rows,
            "checkpoint": json.dumps(checkpointer.storage, default=str),
            "notices": list(model.notices),
            "device_code": fake.codes[room_no],
            "order_events": [
                event for event in reloaded.events if event.get("type") == "credential_delivered"
            ],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    code = values["device_code"]

    # 下发提示只在模型请求里出现：模型据它复述明文，实时增量携带明文（UI 即时可见）
    assert values["notices"] and code in values["notices"][0]
    deltas = [record.data["text"] for record in values["events"] if record.event == "message.delta"]
    assert any(code in text for text in deltas)

    # 落库回复、检查点与完成事件全部打码；工具事实不含明文
    assistant_texts = [text for role, text in values["messages"] if role == "assistant"]
    assert all(code not in text for text in assistant_texts)
    assert any("[已打码]" in text for text in assistant_texts)
    assert code not in values["checkpoint"]
    completed = [
        record.data["text"] for record in values["events"] if record.event == "message.completed"
    ]
    assert completed and all(code not in text for text in completed)
    assert all(code not in str(row) for row in values["audit"])

    # 工具事实与业务事实：一次下发、两代凭证、订单事件留痕
    assert len(values["audit"]) == 1
    tool_name, status, args_digest, idempotency_key = values["audit"][0]
    assert tool_name == "deliver_room_password"
    assert status == "completed"
    assert args_digest == {"order_no": values["order_no"]}
    assert idempotency_key is not None
    assert values["rows"] == [(1, "revoked", "checkin"), (2, "active", "delivery")]
    assert len(values["order_events"]) == 1
    assert code not in str(values["order_events"])
