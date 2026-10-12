"""幂等组件集成测试（真实 PostgreSQL）：唯一约束兜底、回放、并发恰一次、同事务原子性。

覆盖 P2 退出标准中的「重复提交返回既有结果而非新建副作用」（验收主张 A3 前置）。
"""

from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError
from app.models.domain import HousekeepingTask, IdempotencyRecord, Order, Room, RoomType
from app.repositories.guest_repository import GuestRepository
from app.repositories.idempotency_repository import IdempotencyRepository
from app.repositories.room_repository import RoomRepository, RoomTypeRepository
from app.services.housekeeping_service import HousekeepingService
from app.services.idempotency_service import IdempotencyService
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


async def _count_records(session: AsyncSession, scope: str, key: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(IdempotencyRecord)
                .where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
            )
        ).scalar_one()
    )


async def _count_room_types(session: AsyncSession, code: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(RoomType).where(RoomType.code == code)
            )
        ).scalar_one()
    )


def test_repeated_submission_replays_existing_result(runtime_db_url: str) -> None:
    scope = "room_type.create"
    key = f"submit-{uuid4().hex[:8]}"
    code = f"RT-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> dict[str, object]:
        service = IdempotencyService(session)
        calls: list[str] = []

        async def operation() -> tuple[str, UUID]:
            calls.append("called")
            row = await RoomTypeRepository(session).create(
                code=code, name="大床房", base_price_fen=39800, capacity=2, bed_type="king"
            )
            return ("room_type", row.id)

        first = await service.run(scope, key, operation)
        await session.commit()
        second = await service.run(scope, key, operation)
        await session.commit()
        return {
            "first": first,
            "second": second,
            "calls": calls,
            "records": await _count_records(session, scope, key),
            "room_types": await _count_room_types(session, code),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["first"].replayed is False
    assert values["second"].replayed is True
    assert values["first"].result_ref == values["second"].result_ref
    assert values["first"].result_type == values["second"].result_type == "room_type"
    assert values["calls"] == ["called"]
    assert values["records"] == 1
    assert values["room_types"] == 1


def test_concurrent_submissions_execute_side_effect_exactly_once(runtime_db_url: str) -> None:
    scope = "lock.rotate"
    key = f"rotate-{uuid4().hex[:8]}"
    code = f"RT-{uuid4().hex[:8]}"

    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, object]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def winner() -> object:
            async with factory() as session:
                service = IdempotencyService(session)

                async def operation() -> tuple[str, UUID]:
                    started.set()
                    await release.wait()
                    row = await RoomTypeRepository(session).create(
                        code=code,
                        name="大床房",
                        base_price_fen=39800,
                        capacity=2,
                        bed_type="king",
                    )
                    return ("room_type", row.id)

                outcome = await service.run(scope, key, operation)
                await session.commit()
                return outcome

        async def loser() -> object:
            async with factory() as session:
                await started.wait()
                service = IdempotencyService(session)

                async def operation() -> tuple[str, UUID]:
                    raise AssertionError("竞争失败方不得执行第二次副作用")

                outcome = await service.run(scope, key, operation)
                await session.commit()
                return outcome

        winner_task = asyncio.create_task(winner())
        loser_task = asyncio.create_task(loser())
        await started.wait()
        await asyncio.sleep(0.3)
        release.set()
        outcomes = await asyncio.gather(winner_task, loser_task)

        async with factory() as session:
            return {
                "replayed": sorted(outcome.replayed for outcome in outcomes),
                "refs": {outcome.result_ref for outcome in outcomes},
                "records": await _count_records(session, scope, key),
                "room_types": await _count_room_types(session, code),
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["replayed"] == [False, True]
    assert len(values["refs"]) == 1
    assert values["records"] == 1
    assert values["room_types"] == 1


def test_record_and_side_effect_share_one_transaction(runtime_db_url: str) -> None:
    scope = "order.create"
    key = f"rollback-{uuid4().hex[:8]}"
    code = f"RT-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> dict[str, object]:
        service = IdempotencyService(session)

        async def operation() -> tuple[str, UUID]:
            row = await RoomTypeRepository(session).create(
                code=code, name="大床房", base_price_fen=39800, capacity=2, bed_type="king"
            )
            return ("room_type", row.id)

        await service.run(scope, key, operation)
        await session.rollback()
        return {
            "records": await _count_records(session, scope, key),
            "room_types": await _count_room_types(session, code),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["records"] == 0
    assert values["room_types"] == 0


def test_claim_without_result_fails_fast(runtime_db_url: str) -> None:
    scope = "wechat.pay_notify"
    key = f"notify-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> dict[str, object]:
        await IdempotencyRepository(session).create(scope=scope, key=key)
        await session.commit()

        service = IdempotencyService(session)
        executed: list[str] = []

        async def operation() -> tuple[str, UUID]:
            executed.append("called")
            return ("order", uuid4())

        error: str | None = None
        try:
            await service.run(scope, key, operation)
        except ConflictError as exc:
            error = str(exc)
        return {"error": error, "executed": executed}

    values = run_with_session(runtime_db_url, _body)
    assert values["error"] is not None and "结果未落库" in str(values["error"])
    assert values["executed"] == []


def test_order_creation_replays_by_idempotency_key(runtime_db_url: str) -> None:
    key = f"order-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        guest = await GuestRepository(session).create(nickname="测试客人")
        service = OrderService(session)
        first = await service.create(
            order_no=f"NO-{uuid4().hex[:10]}",
            guest_id=guest.id,
            room_type_id=room.room_type_id,
            check_in_date=date(2026, 10, 1),
            check_out_date=date(2026, 10, 3),
            amount_fen=79600,
            idempotency_key=key,
        )
        await session.commit()
        second = await service.create(
            order_no=f"NO-{uuid4().hex[:10]}",
            guest_id=guest.id,
            room_type_id=room.room_type_id,
            check_in_date=date(2026, 10, 1),
            check_out_date=date(2026, 10, 3),
            amount_fen=79600,
            idempotency_key=key,
        )
        await session.commit()
        total = int(
            (
                await session.execute(
                    select(func.count()).select_from(Order).where(Order.idempotency_key == key)
                )
            ).scalar_one()
        )
        return {
            "first_id": first.id,
            "first_no": first.order_no,
            "second_id": second.id,
            "second_no": second.order_no,
            "total": total,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["second_id"] == values["first_id"]
    assert values["second_no"] == values["first_no"]
    assert values["total"] == 1


def test_housekeeping_creation_replays_by_idempotency_key(runtime_db_url: str) -> None:
    key = f"dispatch-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> dict[str, object]:
        room = await _seed_room(session)
        service = HousekeepingService(session)
        first = await service.create(
            task_type="call_clean",
            room_id=room.id,
            room_no=room.room_no,
            idempotency_key=key,
        )
        await session.commit()
        second = await service.create(
            task_type="call_clean",
            room_id=room.id,
            room_no=room.room_no,
            idempotency_key=key,
        )
        await session.commit()
        total = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(HousekeepingTask)
                    .where(HousekeepingTask.idempotency_key == key)
                )
            ).scalar_one()
        )
        return {"first_id": first.id, "second_id": second.id, "total": total}

    values = run_with_session(runtime_db_url, _body)
    assert values["second_id"] == values["first_id"]
    assert values["total"] == 1
