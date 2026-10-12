"""种子数据集成测试：幂等可重放，演示主链路（入住中订单 + 有效凭证 + 派单）可复现。

集成库为会话级共享，其它用例可能已建同名房间/账号，因此新增量以运行前后快照差为准。
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import (
    Guest,
    HousekeepingTask,
    LockCredential,
    Order,
    Room,
    RoomType,
    Staff,
)
from app.models.runtime import User
from app.repositories.lock_credential_repository import LockCredentialRepository
from app.seed import (
    ACCOUNT_PLAN,
    DEMO_ORDER_NO,
    DEMO_ROOM_NO,
    DEMO_TASK_DEDUPE_KEY,
    ROOM_PLAN,
    ROOM_TYPES,
    seed,
)
from tests.integration.db_utils import run_with_session

pytestmark = pytest.mark.integration

DEMO_CREDENTIAL_ID = f"DEMO-CRED-{DEMO_ROOM_NO}"


def test_seed_is_idempotent_and_demo_flow_is_reproducible(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, object]:
        async def _count(model: type, *conditions: object) -> int:
            stmt = select(func.count()).select_from(model)
            for condition in conditions:
                stmt = stmt.where(condition)
            return int((await session.execute(stmt)).scalar_one())

        async def _snapshot() -> dict[str, int]:
            guest_user_id = select(User.id).where(User.username == "guest-1001").scalar_subquery()
            return {
                "accounts": await _count(User, User.username.in_([row[0] for row in ACCOUNT_PLAN])),
                "room_types": await _count(
                    RoomType, RoomType.code.in_([spec.code for spec in ROOM_TYPES])
                ),
                "rooms": await _count(Room, Room.room_no.in_([row[0] for row in ROOM_PLAN])),
                "staff": await _count(Staff, Staff.staff_no.in_(["HK-2001", "HK-2002"])),
                "guests": await _count(Guest, Guest.user_id == guest_user_id),
                "orders": await _count(Order, Order.order_no == DEMO_ORDER_NO),
                "lock_credentials": await _count(
                    LockCredential, LockCredential.device_credential_id == DEMO_CREDENTIAL_ID
                ),
                "housekeeping_tasks": await _count(
                    HousekeepingTask, HousekeepingTask.idempotency_key == DEMO_TASK_DEDUPE_KEY
                ),
            }

        before = await _snapshot()
        first = await seed(session)
        after_first = await _snapshot()
        second = await seed(session)
        after_second = await _snapshot()

        order = (
            await session.execute(select(Order).where(Order.order_no == DEMO_ORDER_NO))
        ).scalar_one()
        room = (
            await session.execute(select(Room).where(Room.room_no == DEMO_ROOM_NO))
        ).scalar_one()
        credential = await LockCredentialRepository(session).get_active_for_room(DEMO_ROOM_NO)
        task = (
            await session.execute(
                select(HousekeepingTask).where(
                    HousekeepingTask.idempotency_key == DEMO_TASK_DEDUPE_KEY
                )
            )
        ).scalar_one()
        guest = (
            await session.execute(select(Guest).where(Guest.nickname == "演示客人"))
        ).scalar_one_or_none()

        return {
            "created": {key: after_first[key] - before[key] for key in before},
            "reported": dict(first.created),
            "second": dict(second.created),
            "stable": after_second == after_first,
            "counts": after_second,
            "has_guest": guest is not None,
            "order_status": order.status,
            "order_paid_at": order.paid_at,
            "order_checked_in_at": order.checked_in_at,
            "room_status": room.status,
            "credential_status": credential.status if credential else None,
            "credential_code_hash": credential.code_hash if credential else None,
            "task_status": task.status,
            "task_type": task.task_type,
            "task_due_at": task.due_at,
            "check_in": order.check_in_date < date.today() < order.check_out_date,
        }

    values = run_with_session(runtime_db_url, _body)

    created = {key: value for key, value in values["created"].items() if value}
    assert values["reported"] == created
    assert values["second"] == {}
    assert values["stable"] is True
    assert values["counts"] == {
        "accounts": 6,
        "room_types": 3,
        "rooms": 12,
        "staff": 2,
        "guests": 1,
        "orders": 1,
        "lock_credentials": 1,
        "housekeeping_tasks": 1,
    }
    assert values["has_guest"] is True

    assert values["order_status"] == "checked_in"
    assert values["order_paid_at"] is not None
    assert values["order_checked_in_at"] is not None
    assert values["room_status"] == "occupied"
    assert values["credential_status"] == "active"
    code_hash = str(values["credential_code_hash"])
    assert len(code_hash) == 64 and code_hash.isalnum()
    assert values["task_status"] == "pending"
    assert values["task_type"] == "call_clean"
    assert values["task_due_at"] is not None
    assert values["check_in"] is True
