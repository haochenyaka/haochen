"""订单工具集成测试（真实 PostgreSQL）：执行处复验、幂等重放与审计闭环。

工具层断言以数据库事实为准：归属（订单不属于当前客人）与不存在统一为中性文案，
日期窗 / 容量 / 库存由服务层复验；同一工具调用重放不产生第二次副作用。最后一个
用例走完整 Run 链路（图 → 工具 → 订单 → tool_audit），验证"模型 → 工具 → 事实"闭环。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.context import ResourceSnapshot
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import default_registry
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.models.audit import ToolAudit
from app.models.domain import Invoice, RoomType
from app.repositories import (
    GuestRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.services.audit_service import AuditRecorder
from app.services.order_service import OrderService
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session, run_with_session_factory
from tests.integration.support import RecordingDispatcher, ScriptedToolChatModel, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

NOT_FOUND_TEXT = "没有找到对应的订单，请核对订单号后重试。"


def _future(days: int) -> str:
    return (datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)).isoformat()


async def _call(
    tool_name: str,
    context: RuntimeContext | None,
    *,
    tool_call_id: str | None = None,
    **kwargs: Any,
) -> str:
    tool = default_registry().get(tool_name)
    runtime = SimpleNamespace(context=context, tool_call_id=tool_call_id)
    return await tool.coroutine(runtime=runtime, **kwargs)


async def _guest_user(session: AsyncSession, *, username: str | None = None) -> tuple[UUID, UUID]:
    user = await UserRepository(session).get_or_create(
        username=username or f"order-{uuid4().hex[:8]}"
    )
    guest = await GuestRepository(session).get_or_create_by_user(user.id)
    return user.id, guest.id


async def _seed_room_type(
    session: AsyncSession, *, rooms: int = 1, capacity: int = 2, price_fen: int = 39800
) -> RoomType:
    room_type = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8].upper()}",
        name="大床房",
        base_price_fen=price_fen,
        capacity=capacity,
        bed_type="king",
    )
    room_repo = RoomRepository(session)
    for _ in range(rooms):
        await room_repo.create(
            room_no=f"T{uuid4().hex[:6]}",
            room_type_id=room_type.id,
            floor=3,
            lock_device_no=f"LOCK-{uuid4().hex[:8]}",
        )
    return room_type


def _runtime_context(factory: SessionFactoryArg, *, user_id: UUID, run_id: UUID | None = None):
    return RuntimeContext(
        agent_slug="front-desk",
        user_id=user_id,
        thread_id=uuid4(),
        run_id=run_id or uuid4(),
        actor_id="order-tool-test",
        resources=ResourceSnapshot(),
        config={},
        prompt_docs="",
        checkpointer=None,
        recorder=AuditRecorder(run_id=uuid4(), agent_slug="front-desk", session_factory=factory),
        session_factory=factory,
    )


def test_list_room_types_lists_active_only(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        async with factory() as session:
            user_id, _ = await _guest_user(session)
            active = await _seed_room_type(session, price_fen=39900)
            inactive = await RoomTypeRepository(session).create(
                code=f"RT-{uuid4().hex[:8].upper()}",
                name="下架主题房",
                base_price_fen=9900,
                capacity=2,
                bed_type="queen",
                active=False,
            )
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            return {
                "text": await _call("list_room_types", context),
                "active_code": active.code,
                "inactive_code": inactive.code,
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert f"- {values['active_code']}：大床房，399.00 元/晚，可住 2 人" in values["text"]
    assert values["inactive_code"] not in values["text"]


def test_create_order_creates_booking_for_current_guest(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=2, price_fen=39800)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            text = await _call(
                "create_order",
                context,
                tool_call_id="call-create-1",
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(4),
                guest_count=2,
            )
        async with factory() as session:
            orders = await OrderRepository(session).list_recent_for_guest(guest_id)
        assert len(orders) == 1
        return {
            "text": text,
            "order": orders[0],
            "run_id": str(context.run_id),
        }

    values = run_with_session_factory(runtime_db_url, _body)
    order = values["order"]
    assert values["text"].startswith("预订已创建，请在支付后到店入住。订单信息：")
    assert order.order_no in values["text"]
    assert order.status == "created"
    assert order.nights == 2
    assert order.amount_fen == 79600
    assert order.idempotency_key == f"{values['run_id']}:call-create-1:order.create"


def test_create_order_rejects_invalid_inputs_without_side_effects(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1, capacity=2)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            past = await _call(
                "create_order",
                context,
                room_type_code=room_type.code,
                check_in_date=_future(-1),
                check_out_date=_future(1),
            )
            equal = await _call(
                "create_order",
                context,
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(2),
            )
            bad_format = await _call(
                "create_order",
                context,
                room_type_code=room_type.code,
                check_in_date="明天",
                check_out_date=_future(3),
            )
            unknown = await _call(
                "create_order",
                context,
                room_type_code="NO-SUCH-TYPE",
                check_in_date=_future(2),
                check_out_date=_future(3),
            )
            overflow = await _call(
                "create_order",
                context,
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(3),
                guest_count=5,
            )
        async with factory() as session:
            count = await OrderRepository(session).list_recent_for_guest(guest_id)
        assert count == []
        return {
            "past": past,
            "equal": equal,
            "bad_format": bad_format,
            "unknown": unknown,
            "overflow": overflow,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "入住日期不能早于今天" in values["past"]
    assert "退房日期必须晚于入住日期" in values["equal"]
    assert "格式应为 YYYY-MM-DD" in values["bad_format"]
    assert "房型不存在或不可预订" in values["unknown"]
    assert "最多入住 2 人" in values["overflow"]


def test_create_order_blocks_when_inventory_exhausted(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        async with factory() as session:
            user_id, _ = await _guest_user(session)
            other_user_id, other_guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1)
            await OrderService(session).create_booking(
                guest_id=other_guest_id,
                room_type_code=room_type.code,
                check_in_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=2),
                check_out_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=4),
            )
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            return {
                "text": await _call(
                    "create_order",
                    context,
                    room_type_code=room_type.code,
                    check_in_date=_future(3),
                    check_out_date=_future(4),
                )
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "已订满" in values["text"]
    assert "更换日期或选择其他房型" in values["text"]


def test_create_order_replay_with_same_tool_call_is_idempotent(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            args = {
                "room_type_code": room_type.code,
                "check_in_date": _future(2),
                "check_out_date": _future(4),
            }
            first = await _call("create_order", context, tool_call_id="call-dup-1", **args)
            second = await _call("create_order", context, tool_call_id="call-dup-1", **args)
        async with factory() as session:
            orders = await OrderRepository(session).list_recent_for_guest(guest_id)
        return {"first": first, "second": second, "orders": orders}

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["first"].startswith("预订已创建")
    assert values["second"].startswith("该操作此前已处理，订单当前信息：")
    assert len(values["orders"]) == 1


def test_cancel_order_replays_and_blocks_second_cancel(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            created = await _call(
                "create_order",
                context,
                tool_call_id="call-book-1",
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(4),
            )
        async with factory() as session:
            order = (await OrderRepository(session).list_recent_for_guest(guest_id))[0]
        order_no = order.order_no
        cancel_args: dict[str, Any] = {
            "tool_call_id": "call-cancel-1",
            "order_no": order_no,
            "reason": "行程变更",
        }
        cancelled = await _call("cancel_order", context, **cancel_args)
        replayed = await _call("cancel_order", context, **cancel_args)
        blocked = await _call(
            "cancel_order", context, tool_call_id="call-cancel-2", order_no=order_no
        )
        async with factory() as session:
            fresh = await OrderRepository(session).get_by_id(order.id)
        assert fresh is not None
        return {
            "created": created,
            "cancelled": cancelled,
            "replayed": replayed,
            "blocked": blocked,
            "status": fresh.status,
            "cancel_reason": fresh.cancel_reason,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "已取消" in values["cancelled"]
    assert values["replayed"].startswith("该操作此前已处理，订单当前信息：")
    assert "已取消" in values["replayed"]
    assert "操作失败：非法状态迁移" in values["blocked"]
    assert values["status"] == "cancelled"
    assert values["cancel_reason"] == "行程变更"


def test_reschedule_order_updates_dates_and_amount_once(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1, price_fen=30000)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            await _call(
                "create_order",
                context,
                tool_call_id="call-book-2",
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(3),
            )
        async with factory() as session:
            order_id = (await OrderRepository(session).list_recent_for_guest(guest_id))[0].id
        first = await _call(
            "reschedule_order",
            context,
            tool_call_id="call-move-1",
            order_no=(await _order_no(factory, order_id)),
            new_check_in_date=_future(5),
            new_check_out_date=_future(8),
        )
        replay = await _call(
            "reschedule_order",
            context,
            tool_call_id="call-move-1",
            order_no=(await _order_no(factory, order_id)),
            new_check_in_date=_future(5),
            new_check_out_date=_future(8),
        )
        async with factory() as session:
            fresh = await OrderRepository(session).get_by_id(order_id)
        assert fresh is not None
        return {
            "first": first,
            "replay": replay,
            "nights": fresh.nights,
            "amount_fen": fresh.amount_fen,
            "events": list(fresh.events or []),
            "check_out": fresh.check_out_date,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["nights"] == 3
    assert values["amount_fen"] == 90000
    assert [event["type"] for event in values["events"]] == ["rescheduled"]
    assert values["replay"].startswith("该操作此前已处理，订单当前信息：")
    assert "900.00 元" in values["replay"]


async def _order_no(factory: SessionFactoryArg, order_id: UUID) -> str:
    async with factory() as session:
        order = await OrderRepository(session).get_by_id(order_id)
    assert order is not None
    return order.order_no


def test_renew_order_requires_paid_and_extends_stay(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1, price_fen=30000)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            await _call(
                "create_order",
                context,
                tool_call_id="call-book-3",
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(4),
            )
        async with factory() as session:
            order = (await OrderRepository(session).list_recent_for_guest(guest_id))[0]
            order_id, order_no = order.id, order.order_no
        unpaid = await _call(
            "renew_order",
            context,
            tool_call_id="call-renew-0",
            order_no=order_no,
            new_check_out_date=_future(5),
        )
        async with factory() as session:
            service = OrderService(session)
            await service.transition(order_id, "pending_payment")
            await service.transition(order_id, "paid", extra={"paid_amount_fen": 60000})
            await session.commit()
        renewed = await _call(
            "renew_order",
            context,
            tool_call_id="call-renew-1",
            order_no=order_no,
            new_check_out_date=_future(5),
        )
        earlier = await _call(
            "renew_order",
            context,
            tool_call_id="call-renew-2",
            order_no=order_no,
            new_check_out_date=_future(3),
        )
        async with factory() as session:
            fresh = await OrderRepository(session).get_by_id(order_id)
        assert fresh is not None
        return {
            "unpaid": unpaid,
            "renewed": renewed,
            "earlier": earlier,
            "nights": fresh.nights,
            "amount_fen": fresh.amount_fen,
            "events": [event["type"] for event in list(fresh.events or [])],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "请先完成支付" in values["unpaid"]
    assert "900.00 元" in values["renewed"]
    assert values["nights"] == 3
    assert values["amount_fen"] == 90000
    assert "必须晚于当前退房日期" in values["earlier"]
    assert values["events"] == ["renewed"]


def test_apply_invoice_requires_payment_and_keeps_single_invoice(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            user_id, guest_id = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1, price_fen=30000)
            await session.commit()
            context = _runtime_context(factory, user_id=user_id)
            await _call(
                "create_order",
                context,
                tool_call_id="call-book-4",
                room_type_code=room_type.code,
                check_in_date=_future(2),
                check_out_date=_future(4),
            )
        async with factory() as session:
            order = (await OrderRepository(session).list_recent_for_guest(guest_id))[0]
            order_id, order_no = order.id, order.order_no
        unpaid = await _call(
            "apply_invoice",
            context,
            tool_call_id="call-inv-0",
            order_no=order_no,
            title="某某科技有限公司",
            tax_no="91310000MA1K000000",
        )
        async with factory() as session:
            service = OrderService(session)
            await service.transition(order_id, "pending_payment")
            await service.transition(order_id, "paid", extra={"paid_amount_fen": 60000})
            await session.commit()
        first = await _call(
            "apply_invoice",
            context,
            tool_call_id="call-inv-1",
            order_no=order_no,
            title="某某科技有限公司",
            tax_no="91310000MA1K000000",
        )
        second = await _call(
            "apply_invoice",
            context,
            tool_call_id="call-inv-2",
            order_no=order_no,
            title="某某科技有限公司",
            tax_no="91310000MA1K000000",
        )
        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(Invoice).where(Invoice.order_id == order_id)
            )
            invoice = (
                await session.execute(select(Invoice).where(Invoice.order_id == order_id))
            ).scalar_one()
        return {
            "unpaid": unpaid,
            "first": first,
            "second": second,
            "count": int(count or 0),
            "amount_fen": invoice.amount_fen,
            "title": invoice.title,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "订单完成支付后才能申请发票" in values["unpaid"]
    assert values["first"].startswith("发票申请已受理：")
    assert "某某科技有限公司" in values["first"]
    assert values["second"].startswith("发票申请已受理：")
    assert values["count"] == 1
    assert values["amount_fen"] == 60000
    assert values["title"] == "某某科技有限公司"


def test_order_ownership_and_missing_order_are_neutral(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        async with factory() as session:
            owner_user_id, owner_guest_id = await _guest_user(session)
            other_user_id, _ = await _guest_user(session)
            room_type = await _seed_room_type(session, rooms=1)
            order = await OrderService(session).create_booking(
                guest_id=owner_guest_id,
                room_type_code=room_type.code,
                check_in_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=2),
                check_out_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=4),
            )
            await session.commit()
            owner_context = _runtime_context(factory, user_id=owner_user_id)
            other_context = _runtime_context(factory, user_id=other_user_id)
            foreign = await _call(
                "cancel_order", other_context, tool_call_id="call-x-1", order_no=order.order_no
            )
            missing = await _call("list_my_orders", other_context, tool_call_id="call-x-2")
            unknown = await _call(
                "cancel_order", owner_context, tool_call_id="call-x-3", order_no="H000000000000"
            )
        async with factory() as session:
            fresh = await OrderRepository(session).get_by_id(order.id)
        assert fresh is not None
        return {
            "foreign": foreign,
            "missing": missing,
            "unknown": unknown,
            "status": fresh.status,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["foreign"] == NOT_FOUND_TEXT
    assert values["unknown"] == NOT_FOUND_TEXT
    assert values["missing"] == "当前没有订单记录。"
    assert values["status"] == "created"


def test_run_loop_creates_booking_and_records_tool_audit(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整 Run 链路：模型点名 create_order → 工具复验并落单 → tool_audit 留痕。"""
    username = f"order-loop-{uuid4().hex[:8]}"
    model = ScriptedToolChatModel(
        tool_name="create_order",
        tool_args={
            "room_type_code": "",
            "check_in_date": _future(2),
            "check_out_date": _future(4),
        },
    )
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        async with factory() as session:
            room_type = await _seed_room_type(session, rooms=1, price_fen=30000)
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
            tool_rows = (
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
            "order_status": orders[0].status if orders else None,
            "amount_fen": orders[0].amount_fen if orders else None,
            "tool_rows": [(row.tool_name, row.status) for row in tool_rows],
            "seen_tools": model.seen_tools,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    assert values["orders"] == 1
    assert values["order_status"] == "created"
    assert values["amount_fen"] == 60000
    assert values["tool_rows"] == [("create_order", "completed")]
    # 资源归一化后模型只见声明的 create_order（FrontDeskAgent 的其余工具被剔除）
    assert values["seen_tools"] == [["create_order"], ["create_order"]]


def test_front_desk_declared_tools_are_all_registered() -> None:
    registered = set(default_registry().names())
    assert sorted(set(FrontDeskAgent.tool_ids) - registered) == []
    assert "create_order" in registered
    assert set(FrontDeskAgent.knowledge_base_ids) == {"hotel-guest"}


def test_order_repository_count_overlapping_window(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, int]:
        _, guest_id = await _guest_user(session)
        room_type = await _seed_room_type(session, rooms=1)
        repo = OrderRepository(session)
        await OrderService(session).create_booking(
            guest_id=guest_id,
            room_type_code=room_type.code,
            check_in_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=1),
            check_out_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=3),
        )
        base = datetime.now(HOTEL_TIMEZONE).date()
        overlapping = await repo.count_overlapping(
            room_type_id=room_type.id,
            check_in_date=base + timedelta(days=2),
            check_out_date=base + timedelta(days=4),
        )
        touching = await repo.count_overlapping(
            room_type_id=room_type.id,
            check_in_date=base + timedelta(days=3),
            check_out_date=base + timedelta(days=5),
        )
        return {"overlapping": overlapping, "touching": touching}

    values = run_with_session(runtime_db_url, _body)
    assert values["overlapping"] == 1
    # [check_in, check_out) 半开区间：退房日当天入住不算冲突
    assert values["touching"] == 0
