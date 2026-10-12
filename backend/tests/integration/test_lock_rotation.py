"""P4-T7 每日轮换 / 续订展期 / 泄露处置集成测试（真实 PostgreSQL + 门锁替身）。

覆盖：空置房轮换（今日窗口签发 + 次日 next 预排）、到点 next 转正与旧凭证作废、同日重跑
幂等、漏跑日自愈、在住/在住房态/维保跳过（双口径）、续订只展期不换码、泄露处置（吊销旧码
+ 清空 next + 保持窗口 + 期望版本守卫）、任务级失败关闭与结果无明文、cron 时间窗与去重键
（每天至多一次、终态不自动重放）、管理端泄露处置 API 的角色边界与明文边界（A2/A6）。
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.domain.errors import ConflictError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock import task_handlers as lock_task_handlers
from app.lock.service import MIN_VALID_HOURS, LockCredentialService
from app.main import app
from app.models.task import Task
from app.repositories import OrderRepository, RoomRepository, UserRepository
from app.repositories.task_repository import TaskRepository
from app.routers import admin_lock_router
from app.security import hash_password
from app.services.task_dispatch import NullTaskDispatcher
from app.services.task_registry import resolve_handler
from app.services.task_service import TaskExecutor
from tests.integration.db_utils import run_with_session, run_with_session_factory
from tests.integration.test_lock_credentials import (
    FakeLockClient,
    _credential_rows,
    _events,
    _guest,
    _raw_lock_rows,
    _seed_paid_order,
    _StaticLockClient,
)

pytestmark = pytest.mark.integration

SessionFactory = async_sessionmaker[AsyncSession]

ROTATE_TASK_TYPE = lock_task_handlers.ROTATE_DAILY_TASK_TYPE
ROTATE_DATE_TASK = date(2026, 3, 1)
ROTATE_DATE_FAILURE = date(2026, 3, 2)
ROTATE_DATE_CRON = date(2026, 9, 28)
ROTATE_DATE_MANUAL = date(2026, 1, 5)


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=HOTEL_TIMEZONE)


def _today() -> date:
    return datetime.now(HOTEL_TIMEZONE).date()


def _ensure_rotation_handler_registered() -> None:
    """注册表可能被其他测试重置而模块已缓存：重载重新触发注册，保持用例顺序无关。"""
    if (
        resolve_handler(
            lock_task_handlers.ROTATE_DAILY_TASK_TYPE,
            lock_task_handlers.ROTATE_DAILY_HANDLER_VERSION,
        )
        is not None
    ):
        return
    importlib.reload(lock_task_handlers)
    assert (
        resolve_handler(
            lock_task_handlers.ROTATE_DAILY_TASK_TYPE,
            lock_task_handlers.ROTATE_DAILY_HANDLER_VERSION,
        )
        is not None
    )


async def _rotation_tasks(session: AsyncSession, *, dedupe_key: str) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.dedupe_key == dedupe_key)
        .order_by(Task.created_at, Task.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars())


async def _adopt_device_state(session: AsyncSession, fake: FakeLockClient) -> None:
    """把库中既有凭证灌入替身：轮换任务按全库房间跑批，替身需与本地状态一致。"""
    for room in await RoomRepository(session).list_all():
        rows = await _credential_rows(session, room_no=room.room_no)
        if not rows:
            continue
        fake.versions[room.room_no] = max(row.version for row in rows)
        for row in rows:
            if row.status not in {"active", "next"}:
                continue
            record = {
                "version": row.version,
                "code": f"adopted-{room.room_no}-{row.version}",
                "reason": row.reason,
                "status": row.status,
                "valid_from": row.valid_from,
                "valid_to": row.valid_to,
            }
            slot = fake.current if row.status == "active" else fake.next_slots
            slot[room.room_no] = record
            if row.status == "active":
                fake.codes[room.room_no] = record["code"]


def test_daily_rotation_issues_today_window_and_schedules_next(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        rotation = await LockCredentialService(session).rotate_daily_room(
            room_no=room.room_no, client=fake
        )
        await session.commit()
        return {
            "rotation": rotation,
            "rows": await _credential_rows(session, room_no=room.room_no),
            "raw": await _raw_lock_rows(session, room_no=room.room_no),
            "issued": list(fake.issued),
            "scheduled": list(fake.scheduled),
            "codes": [fake.codes[room.room_no], fake.next_slots[room.room_no]["code"]],
            "room_no": room.room_no,
        }

    values = run_with_session(runtime_db_url, _body)
    rotation = values["rotation"]
    today = _today()
    assert rotation.promoted is False
    assert rotation.version == 1
    assert rotation.valid_from == _day_start(today)
    assert rotation.valid_to == _day_start(today + timedelta(days=1))
    assert rotation.next_version == 2
    assert rotation.next_valid_from == _day_start(today + timedelta(days=1))
    assert rotation.next_valid_to == _day_start(today + timedelta(days=2))

    assert [(row.version, row.status) for row in values["rows"]] == [(1, "active"), (2, "next")]
    assert [call["reason"] for call in values["issued"]] == ["daily"]
    assert [call["room_no"] for call in values["scheduled"]] == [values["room_no"]]

    # 明文零落库（A2）：当前码与预生成码都只存在设备侧
    for plaintext in values["codes"]:
        assert all(plaintext not in raw_row for raw_row in values["raw"])


def test_daily_rotation_promotes_due_next_and_revokes_previous(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        await service.rotate_daily_room(room_no=room.room_no, client=fake)
        await session.commit()
        promoted_code = fake.next_slots[room.room_no]["code"]

        tomorrow = _day_start(_today() + timedelta(days=1)) + timedelta(minutes=5)
        second = await service.rotate_daily_room(room_no=room.room_no, client=fake, now=tomorrow)
        await session.commit()
        return {
            "second": second,
            "rows": await _credential_rows(session, room_no=room.room_no),
            "activations": list(fake.activations),
            "issued": list(fake.issued),
            "scheduled": list(fake.scheduled),
            "codes": (promoted_code, fake.codes[room.room_no]),
            "raw": await _raw_lock_rows(session, room_no=room.room_no),
        }

    values = run_with_session(runtime_db_url, _body)
    second = values["second"]
    tomorrow = _day_start(_today() + timedelta(days=1))
    assert second.promoted is True
    assert second.version == 2
    assert second.valid_from == tomorrow
    assert second.valid_to == tomorrow + timedelta(days=1)
    assert second.next_version == 3
    assert second.next_valid_from == tomorrow + timedelta(days=1)

    assert [(row.version, row.status) for row in values["rows"]] == [
        (1, "revoked"),
        (2, "active"),
        (3, "next"),
    ]
    assert values["activations"] == [{"room_no": values["rows"][0].room_no, "version": 2}]
    assert len(values["issued"]) == 1  # 到点转正不重新签发
    assert len(values["scheduled"]) == 2
    promoted_code, active_code = values["codes"]
    assert promoted_code == active_code  # 转正的是昨日预生成的那把码
    assert all(promoted_code not in raw_row for raw_row in values["raw"])


def test_daily_rotation_rerun_same_day_is_noop(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        moment = _day_start(_today()) + timedelta(minutes=5)
        first = await service.rotate_daily_room(room_no=room.room_no, client=fake, now=moment)
        await session.commit()
        calls_after_first = (len(fake.issued), len(fake.scheduled), len(fake.activations))
        second = await service.rotate_daily_room(
            room_no=room.room_no, client=fake, now=moment + timedelta(minutes=10)
        )
        await session.commit()
        return {
            "first": first,
            "second": second,
            "calls_after_first": calls_after_first,
            "calls_after_second": (len(fake.issued), len(fake.scheduled), len(fake.activations)),
            "rows": await _credential_rows(session, room_no=room.room_no),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["calls_after_first"] == values["calls_after_second"]
    assert values["second"].promoted is False
    assert values["second"].version == values["first"].version == 1
    assert values["second"].next_version == values["first"].next_version == 2
    assert len(values["rows"]) == 2


def test_daily_rotation_heals_after_missed_days(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        stale = _day_start(_today() - timedelta(days=2)) + timedelta(minutes=5)
        await service.rotate_daily_room(room_no=room.room_no, client=fake, now=stale)
        await session.commit()

        healed = await service.rotate_daily_room(
            room_no=room.room_no, client=fake, now=_day_start(_today()) + timedelta(minutes=5)
        )
        await session.commit()
        return {
            "healed": healed,
            "rows": await _credential_rows(session, room_no=room.room_no),
            "issued": list(fake.issued),
            "next_revocations": list(fake.next_revocations),
            "scheduled": list(fake.scheduled),
        }

    values = run_with_session(runtime_db_url, _body)
    healed = values["healed"]
    today = _today()
    assert healed.promoted is False  # 昨日 next 已过期，不转正
    assert healed.version == 3
    assert healed.valid_from == _day_start(today)
    assert healed.next_version == 4
    assert healed.next_valid_from == _day_start(today + timedelta(days=1))
    assert [(row.version, row.status) for row in values["rows"]] == [
        (1, "revoked"),
        (2, "revoked"),
        (3, "active"),
        (4, "next"),
    ]
    assert len(values["issued"]) == 2
    assert len(values["next_revocations"]) == 1
    assert len(values["scheduled"]) == 2


def test_daily_rotation_skips_in_house_and_maintenance(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (vacant, in_house, occupied_room, maintenance) = await _seed_paid_order(
            session,
            guest_id=guest.id,
            room_statuses=("available", "available", "occupied", "maintenance"),
        )
        assert await OrderRepository(session).apply_fields(
            order.id, from_statuses=(order.status,), values={"room_id": in_house.id}
        )
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        plan = await service.plan_daily_rotation(today=_today())

        errors: dict[str, str] = {}
        for label, room in (
            ("in_house", in_house),
            ("occupied_status", occupied_room),
            ("maintenance", maintenance),
        ):
            try:
                await service.rotate_daily_room(room_no=room.room_no, client=fake)
            except ConflictError as exc:
                errors[label] = str(exc)
        return {
            "plan": plan,
            "errors": errors,
            "vacant": vacant.room_no,
            "in_house": in_house.room_no,
            "occupied": occupied_room.room_no,
            "maintenance": maintenance.room_no,
            "device_calls": list(fake.issued) + list(fake.scheduled) + list(fake.activations),
        }

    values = run_with_session(runtime_db_url, _body)
    plan = values["plan"]
    assert values["vacant"] in plan.candidates
    assert set(plan.skipped_occupied) >= {values["in_house"], values["occupied"]}
    assert values["maintenance"] in plan.skipped_maintenance
    assert values["vacant"] not in plan.skipped_occupied

    assert set(values["errors"]) == {"in_house", "occupied_status", "maintenance"}
    assert "在住" in values["errors"]["in_house"]
    assert "在住" in values["errors"]["occupied_status"]  # 房态口径：无订单也不动在住房的码
    assert "维保" in values["errors"]["maintenance"]
    assert values["device_calls"] == []


def test_renewal_extends_window_without_new_code(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, _ = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()
        service = LockCredentialService(session)
        fact = await service.issue_for_order(order_id=order.id, client=fake)
        await session.commit()

        new_checkout = order.check_out_date + timedelta(days=3)
        assert await OrderRepository(session).apply_fields(
            order.id,
            from_statuses=(order.status,),
            values={
                "check_out_date": new_checkout,
                "nights": (new_checkout - order.check_in_date).days,
            },
        )
        await session.commit()
        renewed = await OrderRepository(session).get_by_id(order.id)
        assert renewed is not None

        window = await service.extend_for_renewal(order=renewed, client=fake)
        await session.commit()
        replay = await service.extend_for_renewal(order=renewed, client=fake)
        await session.commit()

        reloaded = await OrderRepository(session).get_by_id(order.id)
        assert reloaded is not None
        return {
            "fact": fact,
            "window": window,
            "replay": replay,
            "extensions": list(fake.extensions),
            "issued": list(fake.issued),
            "rotations": list(fake.rotations),
            "rows": await _credential_rows(session, room_no=fact.room_no),
            "order": reloaded,
            "code": fake.codes[fact.room_no],
        }

    values = run_with_session(runtime_db_url, _body)
    fact, window, replay = values["fact"], values["window"], values["replay"]
    assert window is not None and window.extended is True
    assert window.version == fact.version == 1  # 续订保持当前凭证：不换码、不换版本
    assert window.valid_to > fact.valid_to
    assert window.room_no == fact.room_no

    assert replay is not None and replay.extended is False  # 窗口已覆盖 → 重放不触达设备
    assert len(values["extensions"]) == 1
    assert len(values["issued"]) == 1
    assert values["rotations"] == []

    (row,) = values["rows"]
    assert row.status == "active"
    assert row.version == 1
    assert row.valid_to == window.valid_to

    extended = _events(values["order"], "credential_extended")
    assert len(extended) == 1
    assert extended[0]["version"] == 1
    assert extended[0]["room_no"] == fact.room_no
    assert values["code"] == fact.code  # 客人手里的码不变


def test_renewal_without_credential_is_noop(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, _ = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()
        window = await LockCredentialService(session).extend_for_renewal(order=order, client=fake)
        return {
            "window": window,
            "calls": list(fake.extensions) + list(fake.issued) + list(fake.rotations),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["window"] is None  # 未分配房间：续订事实成立，凭证由既有链路补齐
    assert values["calls"] == []


def test_leak_handling_rotates_and_clears_next(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        first = await service.rotate_daily_room(room_no=room.room_no, client=fake)
        await session.commit()
        leaked_code = fake.codes[room.room_no]

        fact = await service.handle_leak(room_no=room.room_no, client=fake)
        await session.commit()
        return {
            "first": first,
            "fact": fact,
            "leaked_code": leaked_code,
            "rows": await _credential_rows(session, room_no=room.room_no),
            "raw": await _raw_lock_rows(session, room_no=room.room_no),
            "next_slots": dict(fake.next_slots),
            "next_revocations": list(fake.next_revocations),
            "rotations": list(fake.rotations),
        }

    values = run_with_session(runtime_db_url, _body)
    fact, first = values["fact"], values["first"]
    assert fact.reason == "leak"
    assert fact.version == 3
    assert fact.code is not None and fact.code != values["leaked_code"]
    # 保持原窗口：在住客人剩余住宿不受影响
    assert fact.valid_from == first.valid_from
    assert fact.valid_to == first.valid_to

    assert [(row.version, row.status) for row in values["rows"]] == [
        (1, "revoked"),
        (2, "revoked"),
        (3, "active"),
    ]
    assert values["next_slots"] == {}
    assert len(values["next_revocations"]) == 1
    assert values["rotations"][-1]["expected_version"] == 1

    for plaintext in (values["leaked_code"], fact.code):
        assert all(plaintext not in raw_row for raw_row in values["raw"])


def test_leak_expected_version_mismatch_is_rejected(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        await service.rotate_daily_room(room_no=room.room_no, client=fake)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.handle_leak(room_no=room.room_no, client=fake, expected_version=99)
        return {
            "rows": await _credential_rows(session, room_no=room.room_no),
            "rotations": list(fake.rotations),
            "next_revocations": list(fake.next_revocations),
            "next_slots": dict(fake.next_slots),
        }

    values = run_with_session(runtime_db_url, _body)
    assert [(row.version, row.status) for row in values["rows"]] == [(1, "active"), (2, "next")]
    assert values["rotations"] == []  # 版本不符：不触碰设备（防旧消息重放改密）
    assert values["next_revocations"] == []
    assert len(values["next_slots"]) == 1


def test_leak_on_expired_credential_reissues_min_window(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)
        stale = _day_start(_today() - timedelta(days=3)) + timedelta(minutes=5)
        await service.rotate_daily_room(room_no=room.room_no, client=fake, now=stale)
        await session.commit()
        fact = await service.handle_leak(room_no=room.room_no, client=fake)
        await session.commit()
        return {"fact": fact}

    fact = run_with_session(runtime_db_url, _body)["fact"]
    assert fact.valid_to - fact.valid_from == timedelta(hours=MIN_VALID_HOURS)
    assert fact.valid_from >= datetime.now(HOTEL_TIMEZONE) - timedelta(minutes=1)


def test_rotation_task_rotates_vacant_rooms_only(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLockClient()
    _ensure_rotation_handler_registered()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(fake))

    async def _run(factory: SessionFactory) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            order, (vacant_a, vacant_b, in_house, maintenance) = await _seed_paid_order(
                session,
                guest_id=guest.id,
                room_statuses=("available", "available", "available", "maintenance"),
            )
            assert await OrderRepository(session).apply_fields(
                order.id, from_statuses=(order.status,), values={"room_id": in_house.id}
            )
            await session.commit()
            await _adopt_device_state(session, fake)
            submission = await lock_task_handlers.submit_rotation(
                session, rotate_date=ROTATE_DATE_TASK
            )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)

        async with factory() as session:
            task = await TaskRepository(session).get_by_id(submission.task_id)
            assert task is not None
            return {
                "outcome": outcome,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "rooms": {
                    "vacant_a": vacant_a.room_no,
                    "vacant_b": vacant_b.room_no,
                    "in_house": in_house.room_no,
                    "maintenance": maintenance.room_no,
                },
                "rows_a": await _credential_rows(session, room_no=vacant_a.room_no),
                "rows_in_house": await _credential_rows(session, room_no=in_house.room_no),
                "raw_a": await _raw_lock_rows(session, room_no=vacant_a.room_no),
            }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    assert values["status"] == "succeeded"
    assert values["error"] is None

    result = values["result"]
    rooms = values["rooms"]
    assert set(result) == {"rotate_date", "rotated", "skipped"}
    assert result["rotate_date"] == ROTATE_DATE_TASK.isoformat()

    rotated = {entry["room_no"]: entry for entry in result["rotated"]}
    assert {rooms["vacant_a"], rooms["vacant_b"]} <= set(rotated)
    entry = rotated[rooms["vacant_a"]]
    assert set(entry) == {
        "room_no",
        "promoted",
        "version",
        "valid_from",
        "valid_to",
        "next_version",
        "next_valid_from",
        "next_valid_to",
    }
    assert entry["version"] == 1 and entry["next_version"] == 2

    skipped = {item["room_no"]: item["reason"] for item in result["skipped"]}
    assert skipped[rooms["in_house"]] == "occupied"
    assert skipped[rooms["maintenance"]] == "maintenance"

    assert [(row.version, row.status) for row in values["rows_a"]] == [(1, "active"), (2, "next")]
    assert values["rows_in_house"] == []  # 在住房间未被触碰

    plaintext = fake.codes[rooms["vacant_a"]]
    assert plaintext not in json.dumps(result, ensure_ascii=False)  # 任务结果随任务落库
    assert all(plaintext not in raw_row for raw_row in values["raw_a"])


def test_rotation_task_fails_closed_when_device_unavailable(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLockClient()
    fake.issue_error = "unavailable"
    _ensure_rotation_handler_registered()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(fake))

    async def _run(factory: SessionFactory) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            submission = await lock_task_handlers.submit_rotation(
                session, rotate_date=ROTATE_DATE_FAILURE
            )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)
        async with factory() as session:
            task = await TaskRepository(session).get_by_id(submission.task_id)
            assert task is not None
            return {
                "outcome": outcome,
                "status": task.status,
                "error": task.error,
                "result": task.result,
                "rows": await _credential_rows(session, room_no=room.room_no),
            }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "failed"
    assert values["status"] == "failed"
    assert values["result"] is None
    assert "每日轮换存在失败房间" in str(values["error"])
    assert "device_unavailable" in str(values["error"])
    assert fake.issued == []  # 设备不可达后不再触碰后续房间
    assert values["rows"] == []


class _FixedDatetime:
    """替身：把模块内 `datetime.now()` 钉在指定时刻（cron 时间窗判定）。"""

    moment: datetime = datetime(2026, 9, 28, 0, 4, tzinfo=HOTEL_TIMEZONE)

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls.moment


def test_submit_daily_rotation_job_window_and_dedupe(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = lock_task_handlers.daily_rotation_dedupe_key(ROTATE_DATE_CRON)

    async def _run(factory: SessionFactory) -> dict[str, Any]:
        results: dict[str, Any] = {}

        def _at(hour: int, minute: int) -> None:
            _FixedDatetime.moment = datetime(2026, 9, 28, hour, minute, tzinfo=HOTEL_TIMEZONE)

        _at(0, 4)
        results["early"] = await lock_task_handlers.submit_daily_rotation_job(
            {"session_factory": factory}
        )
        _at(0, 5)
        results["first"] = await lock_task_handlers.submit_daily_rotation_job(
            {"session_factory": factory}
        )
        _at(0, 10)
        results["second"] = await lock_task_handlers.submit_daily_rotation_job(
            {"session_factory": factory}
        )

        async with factory() as session:
            tasks = await _rotation_tasks(session, dedupe_key=key)
            results["rows"] = [(task.status, task.dedupe_key) for task in tasks]
            results["payload"] = tasks[0].payload
            assert await TaskRepository(session).cancel_pending(tasks[0].id)
            await session.commit()

        _at(0, 15)
        results["after_terminal"] = await lock_task_handlers.submit_daily_rotation_job(
            {"session_factory": factory}
        )
        manual = await lock_task_handlers.submit_daily_rotation(
            rotate_date=ROTATE_DATE_CRON, session_factory=factory
        )
        async with factory() as session:
            tasks = await _rotation_tasks(session, dedupe_key=key)
            results["manual"] = (str(manual.task_id), manual.deduplicated)
            results["after_manual"] = [(task.status, str(task.id)) for task in tasks]
        return results

    monkeypatch.setattr(lock_task_handlers, "datetime", _FixedDatetime)
    values = run_with_session_factory(runtime_db_url, _run)

    assert values["early"] is None  # 00:05 之前不提交
    assert values["first"] is not None
    assert values["second"] is None  # 同日已有任务行 → 不重复提交
    assert values["rows"] == [("pending", key)]
    assert values["payload"] == {"rotate_date": ROTATE_DATE_CRON.isoformat()}
    assert values["after_terminal"] is None  # 终态（取消/失败）也不自动重放
    assert values["manual"][1] is False  # 人工重跑：终态不阻断新任务
    assert len(values["after_manual"]) == 2
    assert [status for status, _ in values["after_manual"]] == ["cancelled", "pending"]


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[admin_lock_router.get_task_dispatcher] = NullTaskDispatcher
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_role(api: Api, *, role: str, password: str = "pass-123456") -> str:
    username = f"{role}-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(password),
                role=role,
                display_name=role,
            )
            await session.commit()

    asyncio.run(_body())
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _seed_room_with_credentials(api: Api) -> tuple[str, FakeLockClient]:
    """空置房 + 当日轮换：得到 active(v1) 与 next(v2)，供泄露处置与元数据视图取证。"""
    fake = FakeLockClient()

    async def _body() -> str:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            guest = await _guest(session)
            _, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            await LockCredentialService(session).rotate_daily_room(
                room_no=room.room_no, client=fake
            )
            await session.commit()
            return room.room_no

    return asyncio.run(_body()), fake


def test_admin_lock_api_roles_and_plaintext_boundary(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    room_no, fake = _seed_room_with_credentials(api)
    monkeypatch.setattr(admin_lock_router, "LockClient", _StaticLockClient(fake))
    admin_token = _seed_role(api, role="admin")
    manager_token = _seed_role(api, role="manager")
    staff_token = _seed_role(api, role="staff")

    metadata = api.client.get(
        f"/api/admin/lock/rooms/{room_no}/credentials", headers=_auth(manager_token)
    )
    assert metadata.status_code == 200, metadata.text
    body = metadata.json()
    assert body["room_no"] == room_no
    assert body["active"]["version"] == 1 and body["active"]["status"] == "active"
    assert body["next"]["version"] == 2 and body["next"]["status"] == "next"
    assert "code" not in metadata.text and "code_hash" not in metadata.text

    leaked = api.client.post(
        f"/api/admin/lock/rooms/{room_no}/credential/rotate", headers=_auth(manager_token), json={}
    )
    assert leaked.status_code == 200, leaked.text
    payload = leaked.json()
    assert payload["room_no"] == room_no
    assert payload["version"] == 3
    assert payload["code"] == fake.codes[room_no]  # 明文只随本次响应一次性流转
    assert payload["valid_from"] == body["active"]["valid_from"]  # 保持原窗口

    conflict = api.client.post(
        f"/api/admin/lock/rooms/{room_no}/credential/rotate",
        headers=_auth(admin_token),
        json={"expected_version": 1},
    )
    assert conflict.status_code == 409  # 版本不符：拒绝处置（防旧消息重放改密）

    triggered = api.client.post(
        "/api/admin/lock/rotation/run",
        headers=_auth(admin_token),
        json={"rotate_date": ROTATE_DATE_MANUAL.isoformat()},
    )
    assert triggered.status_code == 202, triggered.text
    assert triggered.json()["task_type"] == ROTATE_TASK_TYPE
    assert triggered.json()["rotate_date"] == ROTATE_DATE_MANUAL.isoformat()
    assert triggered.json()["deduplicated"] is False

    for path, method in (
        (f"/api/admin/lock/rooms/{room_no}/credentials", "get"),
        (f"/api/admin/lock/rooms/{room_no}/credential/rotate", "post"),
        ("/api/admin/lock/rotation/run", "post"),
    ):
        forbidden = api.client.request(method, path, headers=_auth(staff_token), json={})
        assert forbidden.status_code == 403, path
        anonymous = api.client.request(method, path, json={})
        assert anonymous.status_code == 401, path

    async def _after() -> dict[str, Any]:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            key = lock_task_handlers.daily_rotation_dedupe_key(ROTATE_DATE_MANUAL)
            return {
                "rows": await _credential_rows(session, room_no=room_no),
                "raw": await _raw_lock_rows(session, room_no=room_no),
                "tasks": await _rotation_tasks(session, dedupe_key=key),
            }

    after = asyncio.run(_after())
    assert [(row.version, row.status) for row in after["rows"]] == [
        (1, "revoked"),
        (2, "revoked"),
        (3, "active"),
    ]
    assert all(payload["code"] not in raw_row for raw_row in after["raw"])
    assert len(after["tasks"]) == 1
    assert after["tasks"][0].status == "pending"
