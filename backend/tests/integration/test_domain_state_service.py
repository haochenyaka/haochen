"""状态机服务集成测试（真实 PostgreSQL）：行锁串行化、时间戳副作用与留证门槛。

覆盖 P2 退出标准：非法迁移被拒且数据不变；并发迁移恰好一次生效。
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models.domain import Room
from app.repositories.guest_repository import GuestRepository
from app.repositories.housekeeping_repository import HousekeepingTaskRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.room_repository import RoomRepository, RoomTypeRepository
from app.repositories.staff_repository import StaffRepository
from app.services.housekeeping_service import HousekeepingService
from app.services.order_service import OrderService
from tests.integration.db_utils import run_with_session, run_with_session_factory

pytestmark = pytest.mark.integration


async def _seed_room(session: AsyncSession, *, room_no: str | None = None) -> Room:
    room_type = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8]}",
        name="大床房",
        base_price_fen=39800,
        capacity=2,
        bed_type="king",
    )
    return await RoomRepository(session).create(
        room_no=room_no or f"T{uuid4().hex[:6]}",
        room_type_id=room_type.id,
        floor=3,
        lock_device_no=f"LOCK-{uuid4().hex[:8]}",
    )


async def _create_order(session: AsyncSession, *, room_type_id: UUID) -> UUID:
    guest = await GuestRepository(session).create(nickname="测试客人")
    order = await OrderService(session).create(
        order_no=f"NO-{uuid4().hex[:10]}",
        guest_id=guest.id,
        room_type_id=room_type_id,
        check_in_date=date(2026, 10, 1),
        check_out_date=date(2026, 10, 3),
        amount_fen=79600,
    )
    return order.id


def test_order_legal_chain_records_timestamps_and_events(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        order_id = await _create_order(session, room_type_id=room.room_type_id)
        svc = OrderService(session)

        await svc.transition(order_id, "pending_payment")
        await svc.transition(order_id, "paid", extra={"paid_amount_fen": 79600})
        await svc.transition(order_id, "checked_in")
        await svc.transition(order_id, "checkout_pending")
        completed = await svc.transition(
            order_id, "completed", event={"type": "checkout", "note": "退房完成"}
        )
        assert completed.status == "completed"
        await session.commit()
        return {
            "paid_at": completed.paid_at,
            "checked_in_at": completed.checked_in_at,
            "completed_at": completed.completed_at,
            "cancelled_at": completed.cancelled_at,
            "paid_amount_fen": completed.paid_amount_fen,
            "nights": completed.nights,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["paid_at"] is not None
    assert values["checked_in_at"] is not None
    assert values["completed_at"] is not None
    assert values["cancelled_at"] is None
    assert values["paid_amount_fen"] == 79600
    assert values["nights"] == 2


def test_order_events_append_without_overwrite(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> list[dict[str, object]]:
        room = await _seed_room(session)
        order_id = await _create_order(session, room_type_id=room.room_type_id)
        svc = OrderService(session)
        await svc.transition(order_id, "pending_payment")
        await svc.transition(order_id, "paid", event={"type": "paid", "channel": "wechat"})
        refreshed = await svc.transition(
            order_id, "checked_in", event={"type": "checked_in", "credential_version": 1}
        )
        assert refreshed.events == [
            {"type": "paid", "channel": "wechat"},
            {"type": "checked_in", "credential_version": 1},
        ]
        await session.commit()
        return list(refreshed.events)

    events = run_with_session(runtime_db_url, _body)
    assert [event["type"] for event in events] == ["paid", "checked_in"]


def test_order_cancel_branch_sets_reason_and_timestamp(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        order_id = await _create_order(session, room_type_id=room.room_type_id)
        svc = OrderService(session)
        await svc.transition(order_id, "pending_payment")
        cancelled = await svc.transition(order_id, "cancelled", reason="客人行程变更")
        assert cancelled.cancelled_at is not None
        assert cancelled.cancel_reason == "客人行程变更"
        await session.commit()
        return {"status": cancelled.status, "cancelled_at": cancelled.cancelled_at}

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "cancelled"
    assert values["cancelled_at"] is not None


def test_order_illegal_transition_rejected_without_side_effects(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        order_id = await _create_order(session, room_type_id=room.room_type_id)
        svc = OrderService(session)
        await session.commit()

        with pytest.raises(ConflictError):
            await svc.transition(order_id, "checked_in")
        await session.rollback()

        fresh = await OrderRepository(session).get_by_id(order_id)
        assert fresh is not None
        assert fresh.status == "created"
        assert fresh.checked_in_at is None
        assert fresh.paid_at is None

        await svc.transition(order_id, "pending_payment")
        await svc.transition(order_id, "paid")
        await svc.transition(order_id, "checked_in")
        await svc.transition(order_id, "checkout_pending")
        await svc.transition(order_id, "completed")
        with pytest.raises(ConflictError):
            await svc.transition(order_id, "cancelled")
        await session.commit()

        terminal = await OrderRepository(session).get_by_id(order_id)
        assert terminal is not None
        return {"status": terminal.status, "cancelled_at": terminal.cancelled_at}

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "completed"
    assert values["cancelled_at"] is None


def test_order_transition_missing_order(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> None:
        with pytest.raises(NotFoundError):
            await OrderService(session).transition(uuid4(), "cancelled")

    run_with_session(runtime_db_url, _body)


def test_order_create_validates_dates_and_amount(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> None:
        room = await _seed_room(session)
        guest = await GuestRepository(session).create(nickname="校验客人")
        svc = OrderService(session)
        with pytest.raises(ValidationFailedError):
            await svc.create(
                order_no=f"NO-{uuid4().hex[:10]}",
                guest_id=guest.id,
                room_type_id=room.room_type_id,
                check_in_date=date(2026, 10, 3),
                check_out_date=date(2026, 10, 3),
                amount_fen=100,
            )
        with pytest.raises(ValidationFailedError):
            await svc.create(
                order_no=f"NO-{uuid4().hex[:10]}",
                guest_id=guest.id,
                room_type_id=room.room_type_id,
                check_in_date=date(2026, 10, 1),
                check_out_date=date(2026, 10, 3),
                amount_fen=-1,
            )

    run_with_session(runtime_db_url, _body)


def test_order_concurrent_transition_exactly_one_wins(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> tuple[UUID, list[str]]:
        async with factory() as session:
            room = await _seed_room(session)
            order_id = await _create_order(session, room_type_id=room.room_type_id)
            svc = OrderService(session)
            await svc.transition(order_id, "pending_payment")
            await svc.transition(order_id, "paid")
            await session.commit()

        outcomes: list[str] = []

        async def _attempt() -> None:
            async with factory() as session:
                try:
                    await OrderService(session).transition(order_id, "checked_in")
                except ConflictError:
                    await session.rollback()
                    outcomes.append("conflict")
                    return
                await session.commit()
                outcomes.append("ok")

        await asyncio.gather(_attempt(), _attempt())
        return order_id, outcomes

    order_id, outcomes = run_with_session_factory(runtime_db_url, _body)
    assert sorted(outcomes) == ["conflict", "ok"]

    async def _read(session: AsyncSession) -> dict[str, object]:
        order = await OrderRepository(session).get_by_id(order_id)
        assert order is not None
        return {"status": order.status, "checked_in_at": order.checked_in_at}

    values = run_with_session(runtime_db_url, _read)
    assert values["status"] == "checked_in"
    assert values["checked_in_at"] is not None


def test_checkout_clean_requires_password_rotation_evidence(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        svc = HousekeepingService(session)
        task = await svc.create(
            task_type="checkout_clean",
            room_id=room.id,
            room_no=room.room_no,
            payload={"scene": "退房清洁"},
        )
        task_id = task.id
        await svc.transition(task_id, "accepted")
        await svc.transition(task_id, "in_progress")
        await svc.transition(task_id, "done", evidence={"photo_url": "s3://evidence/1.jpg"})
        await session.commit()

        with pytest.raises(ConflictError):
            await svc.transition(task_id, "verified")
        await session.rollback()

        blocked = await HousekeepingTaskRepository(session).get_by_id(task_id)
        assert blocked is not None
        assert blocked.status == "done"
        assert blocked.verified_at is None
        assert blocked.evidence == {"photo_url": "s3://evidence/1.jpg"}

        verified = await svc.transition(
            task_id, "verified", evidence={"password_rotated": True, "operator_note": "已改密"}
        )
        assert verified.verified_at is not None
        closed = await svc.transition(task_id, "closed")
        assert closed.closed_at is not None
        await session.commit()
        return {
            "status": closed.status,
            "verified_at": verified.verified_at,
            "closed_at": closed.closed_at,
            "evidence": dict(closed.evidence or {}),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "closed"
    assert values["verified_at"] is not None
    assert values["closed_at"] is not None
    assert values["evidence"] == {
        "photo_url": "s3://evidence/1.jpg",
        "password_rotated": True,
        "operator_note": "已改密",
    }


def test_checkout_clean_accepts_rotation_flag_stored_at_done(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        svc = HousekeepingService(session)
        task = await svc.create(task_type="checkout_clean", room_id=room.id, room_no=room.room_no)
        task_id = task.id
        await svc.transition(task_id, "accepted")
        await svc.transition(task_id, "in_progress")
        await svc.transition(task_id, "done", evidence={"password_rotated": True})
        verified = await svc.transition(task_id, "verified")
        await session.commit()
        return {"status": verified.status, "verified_at": verified.verified_at}

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "verified"
    assert values["verified_at"] is not None


def test_call_clean_verify_without_evidence_and_escalated_path(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        staff = await StaffRepository(session).create(staff_no=f"HK-{uuid4().hex[:6]}", name="王芳")
        svc = HousekeepingService(session)

        task = await svc.create(task_type="call_clean", room_id=room.id, room_no=room.room_no)
        task_id = task.id
        escalated = await svc.transition(task_id, "escalated")
        assert escalated.escalated_at is not None

        resumed = await svc.transition(task_id, "in_progress", assigned_staff_id=staff.id)
        assert resumed.assigned_staff_id == staff.id
        await svc.transition(task_id, "done")
        await svc.transition(task_id, "verified")
        closed = await svc.transition(task_id, "closed")
        await session.commit()
        return {
            "status": closed.status,
            "escalated_at": closed.escalated_at,
            "closed_at": closed.closed_at,
            "assigned_staff_id": closed.assigned_staff_id,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "closed"
    assert values["escalated_at"] is not None
    assert values["closed_at"] is not None
    assert values["assigned_staff_id"] is not None


def test_housekeeping_illegal_transition_leaves_data_unchanged(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        svc = HousekeepingService(session)
        task = await svc.create(task_type="call_clean", room_id=room.id, room_no=room.room_no)
        task_id = task.id
        await session.commit()

        with pytest.raises(ConflictError):
            await svc.transition(task_id, "done")
        await session.rollback()

        fresh = await HousekeepingTaskRepository(session).get_by_id(task_id)
        assert fresh is not None
        return {
            "status": fresh.status,
            "done_at": fresh.done_at,
            "accepted_at": fresh.accepted_at,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "pending"
    assert values["done_at"] is None
    assert values["accepted_at"] is None


def test_housekeeping_rejects_plaintext_and_unknown_types(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> None:
        room = await _seed_room(session)
        svc = HousekeepingService(session)
        with pytest.raises(ValidationFailedError):
            await svc.create(task_type="deep_clean", room_id=room.id, room_no=room.room_no)
        with pytest.raises(ValidationFailedError):
            await svc.create(
                task_type="checkout_clean",
                room_id=room.id,
                room_no=room.room_no,
                payload={"password": "123456"},
            )

        task = await svc.create(task_type="checkout_clean", room_id=room.id, room_no=room.room_no)
        with pytest.raises(ValidationFailedError):
            await svc.transition(task.id, "accepted", evidence={"new_password": "123456"})

        with pytest.raises(NotFoundError):
            await svc.transition(uuid4(), "accepted")

    run_with_session(runtime_db_url, _body)
