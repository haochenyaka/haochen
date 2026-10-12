"""领域 Schema 基线验证：十二张表、凭证部分唯一索引、users 扩展列与最小写入回路。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import (
    Guest,
    HousekeepingTask,
    IdempotencyRecord,
    LockCredential,
    Order,
    Room,
    RoomType,
    Staff,
)
from app.models.runtime import User
from tests.integration.db_utils import inspect_tables, run_with_connection, run_with_session

pytestmark = pytest.mark.integration

DOMAIN_TABLES = {
    "room_types",
    "rooms",
    "lock_credentials",
    "orders",
    "housekeeping_tasks",
    "robot_deliveries",
    "attendance_checkins",
    "invoices",
    "complaints",
    "guests",
    "staff",
    "guest_memories",
    "idempotency_records",
}


def test_domain_tables_created(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    assert DOMAIN_TABLES <= set(tables)


def test_users_extended_columns(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    columns = tables["users"]["columns"]
    assert "password_hash" in columns
    assert "status" in columns
    assert "last_login_at" in columns


def test_credential_partial_unique_indexes(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    indexes = tables["lock_credentials"]["indexes"]
    assert indexes["uq_lock_credentials_active_room"]["unique"] is True
    assert "active" in str(
        indexes["uq_lock_credentials_active_room"]["dialect_options"]["postgresql_where"]
    )
    assert indexes["uq_lock_credentials_next_room"]["unique"] is True
    assert "next" in str(
        indexes["uq_lock_credentials_next_room"]["dialect_options"]["postgresql_where"]
    )
    assert "uq_lock_credentials_room_version" in tables["lock_credentials"]["unique_constraints"]

    orders = tables["orders"]["indexes"]
    assert "ix_orders_room_status" in orders
    tasks = tables["housekeeping_tasks"]["indexes"]
    assert "ix_housekeeping_tasks_staff_status" in tasks


async def _seed_room(session: AsyncSession, *, room_no: str) -> Room:
    room_type = RoomType(
        code=f"T-{room_no}",
        name="大床房",
        base_price_fen=29900,
        capacity=2,
        bed_type="king",
    )
    session.add(room_type)
    await session.flush()
    room = Room(room_no=room_no, room_type_id=room_type.id, floor=3, lock_device_no=f"L-{room_no}")
    session.add(room)
    return room


def test_single_active_credential_per_room(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        room = await _seed_room(session, room_no="301")
        await session.flush()

        now = datetime.now(UTC)
        window = {"valid_from": now, "valid_to": now + timedelta(days=1)}
        session.add(
            LockCredential(
                room_id=room.id,
                room_no=room.room_no,
                code_hash="hash-v1",
                status="active",
                reason="checkin",
                version=1,
                **window,
            )
        )
        await session.flush()

        # 同房间第二条 active 被部分唯一索引拒绝
        session.add(
            LockCredential(
                room_id=room.id,
                room_no=room.room_no,
                code_hash="hash-v2",
                status="active",
                reason="leak",
                version=2,
                **window,
            )
        )
        active_conflict = False
        try:
            await session.flush()
        except IntegrityError:
            active_conflict = True
            await session.rollback()

        return {"active_conflict": active_conflict}

    assert run_with_session(runtime_db_url, _body) == {"active_conflict": True}


def test_next_credential_coexists_with_active(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> list[str]:
        room = await _seed_room(session, room_no="302")
        await session.flush()
        now = datetime.now(UTC)
        window = {"valid_from": now, "valid_to": now + timedelta(days=1)}
        session.add(
            LockCredential(
                room_id=room.id,
                room_no=room.room_no,
                code_hash="hash-active",
                status="active",
                reason="checkin",
                version=1,
                **window,
            )
        )
        session.add(
            LockCredential(
                room_id=room.id,
                room_no=room.room_no,
                code_hash="hash-next",
                status="next",
                reason="daily",
                version=2,
                valid_from=now + timedelta(days=1),
                valid_to=now + timedelta(days=2),
            )
        )
        await session.commit()

        rows = (
            await session.execute(
                select(LockCredential).where(LockCredential.room_no == room.room_no)
            )
        ).scalars()
        return sorted(row.status for row in rows)

    assert run_with_session(runtime_db_url, _body) == ["active", "next"]


def test_order_and_task_roundtrip_defaults(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        user = User(username="guest-schema-1", display_name="客人")
        session.add(user)
        await session.flush()
        guest = Guest(user_id=user.id, nickname="张伟")
        session.add(guest)
        staff = Staff(staff_no="HK-S1", name="王芳")
        session.add(staff)
        room = await _seed_room(session, room_no="401")
        await session.flush()

        order = Order(
            order_no="HC20260927001",
            guest_id=guest.id,
            room_id=room.id,
            room_type_id=room.room_type_id,
            check_in_date=date(2026, 10, 1),
            check_out_date=date(2026, 10, 2),
            nights=1,
            amount_fen=29900,
        )
        session.add(order)
        await session.flush()

        task = HousekeepingTask(
            task_type="checkout_clean",
            room_id=room.id,
            room_no=room.room_no,
            order_id=order.id,
            assigned_staff_id=staff.id,
        )
        session.add(task)
        await session.commit()

        return {
            "order_status": order.status,
            "order_events": order.events,
            "task_status": task.status,
            "guest_user_id": guest.user_id,
            "staff_status": staff.status,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["order_status"] == "created"
    assert values["order_events"] == []
    assert values["task_status"] == "pending"
    assert values["staff_status"] == "active"
    assert values["guest_user_id"] is not None


def test_idempotency_scope_key_unique(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> bool:
        session.add(IdempotencyRecord(scope="order.create", key="guest-1:req-1"))
        await session.commit()

        session.add(IdempotencyRecord(scope="order.create", key="guest-1:req-1"))
        conflicted = False
        try:
            await session.commit()
        except IntegrityError:
            conflicted = True
            await session.rollback()
        return conflicted

    assert run_with_session(runtime_db_url, _body) is True
