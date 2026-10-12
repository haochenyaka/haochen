"""P4-T3 门锁凭证服务集成测试（真实 PostgreSQL + 门锁替身）：

覆盖：入住签发分配房间且库中只存 code_hash（明文零落库，A2 负例：raw 行扫描）；
幂等重放不二次调用设备、不重复下发明文；未支付订单与无可用房间拒绝签发；
设备失败整体回滚（订单不进入后续状态）；轮换/吊销携带期望版本（旧版本重放被拒，契约 §2.3）；
`checkin.prepare` 经真实任务执行器端到端成功，任务结果不含明文；
注入门锁失败 → 任务失败关闭且订单保留 paid + checkin_blocked 事件（P4 退出负例）。
"""

from __future__ import annotations

import hashlib
import importlib
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.errors import ConflictError, NotFoundError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock import task_handlers as lock_task_handlers
from app.lock.client import (
    CredentialMetadata,
    IssuedCredential,
    LockError,
    LockUnavailableError,
    UnlockRecord,
)
from app.lock.service import LockCredentialService, checkin_idempotency_key
from app.models.domain import Guest, LockCredential, Order, Room
from app.repositories import (
    GuestRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.repositories.task_repository import TaskRepository
from app.services.order_service import OrderService
from app.services.task_registry import resolve_handler
from app.services.task_service import TaskExecutor, submit_task
from tests.integration.db_utils import run_with_session, run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_VALID_HOURS = 24
"""替身缺省窗口时长：与设备模拟器 DEFAULT_VALID_HOURS 一致（契约 §2.1）。"""


class FakeLockClient:
    """门锁替身：按房间维护版本计数器、当前凭证与 next 槽位，逐条记录调用（契约 §2 形状）。

    版本单调递增（吊销/清空 next 都不回退），`codes` 始终是当前生效凭证的明文——
    预生成的 next 明文只存在 `next_slots` 内，与"客人手里的码"语义一致。
    """

    def __init__(self) -> None:
        self.issue_error: str | None = None  # None | "unavailable" | "rejected"
        self.versions: dict[str, int] = {}
        self.codes: dict[str, str] = {}
        self.current: dict[str, dict[str, Any]] = {}
        self.next_slots: dict[str, dict[str, Any]] = {}
        self.issued: list[dict[str, Any]] = []
        self.rotations: list[dict[str, Any]] = []
        self.revocations: list[dict[str, Any]] = []
        self.scheduled: list[dict[str, Any]] = []
        self.activations: list[dict[str, Any]] = []
        self.next_revocations: list[dict[str, Any]] = []
        self.extensions: list[dict[str, Any]] = []
        self.unlock_queries: list[str | None] = []
        self.records: list[UnlockRecord] = []

    @classmethod
    def from_settings(cls) -> FakeLockClient:
        return cls()

    async def aclose(self) -> None:
        return None

    def _fail_if_injected(self) -> None:
        if self.issue_error == "unavailable":
            raise LockUnavailableError("门锁服务不可达：fake")
        if self.issue_error == "rejected":
            raise LockError("门锁服务返回错误：fake", status_code=500)

    def _next_code(self, room_no: str) -> tuple[int, str]:
        version = self.versions.get(room_no, 0) + 1
        self.versions[room_no] = version
        code = f"{room_no}-{uuid4().hex[:6]}"
        return version, code

    def _guard(self, room_no: str, expected_version: int | None) -> None:
        """期望版本对齐设备侧「当前生效凭证」；无当前凭证时退回最新计数器（吊销后重放）。"""
        if expected_version is None:
            return
        current = self.current.get(room_no)
        latest = current["version"] if current is not None else self.versions.get(room_no)
        if latest != expected_version:
            raise LockError("凭证版本不匹配", status_code=409)

    def _window(
        self,
        *,
        valid_hours: int | None,
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> tuple[datetime, datetime]:
        start = valid_from or datetime.now(HOTEL_TIMEZONE)
        end = valid_to or start + timedelta(hours=valid_hours or DEFAULT_VALID_HOURS)
        return start, end

    def _credential(
        self,
        *,
        room_no: str,
        version: int,
        code: str,
        reason: str,
        status: str = "active",
        valid_hours: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> IssuedCredential:
        start, end = self._window(valid_hours=valid_hours, valid_from=valid_from, valid_to=valid_to)
        return IssuedCredential(
            credential_id=f"devcred-{room_no}-{version}",
            room_no=room_no,
            code=code,
            version=version,
            status=status,
            reason=reason,
            valid_from=start,
            valid_to=end,
        )

    def _metadata(
        self, issued: IssuedCredential, *, status: str | None = None
    ) -> CredentialMetadata:
        return CredentialMetadata(
            credential_id=issued.credential_id,
            room_no=issued.room_no,
            version=issued.version,
            status=status or issued.status,
            reason=issued.reason,
            valid_from=issued.valid_from,
            valid_to=issued.valid_to,
        )

    def _record(self, issued: IssuedCredential, code: str) -> dict[str, Any]:
        return {
            "version": issued.version,
            "code": code,
            "reason": issued.reason,
            "status": issued.status,
            "valid_from": issued.valid_from,
            "valid_to": issued.valid_to,
        }

    async def issue_credential(
        self,
        *,
        room_no: str,
        valid_hours: int | None = None,
        reason: str = "checkin",
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> IssuedCredential:
        self._fail_if_injected()
        version, code = self._next_code(room_no)
        self.issued.append({"room_no": room_no, "version": version, "reason": reason})
        issued = self._credential(
            room_no=room_no,
            version=version,
            code=code,
            reason=reason,
            valid_hours=valid_hours,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.current[room_no] = self._record(issued, code)
        self.codes[room_no] = code
        return issued

    async def rotate_credential(
        self,
        *,
        room_no: str,
        valid_hours: int | None = None,
        reason: str = "daily",
        expected_version: int | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> IssuedCredential:
        self._fail_if_injected()
        self._guard(room_no, expected_version)
        version, code = self._next_code(room_no)
        self.rotations.append(
            {"room_no": room_no, "version": version, "expected_version": expected_version}
        )
        issued = self._credential(
            room_no=room_no,
            version=version,
            code=code,
            reason=reason,
            valid_hours=valid_hours,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.current[room_no] = self._record(issued, code)
        self.codes[room_no] = code
        return issued

    async def schedule_next_credential(
        self,
        *,
        room_no: str,
        valid_from: datetime,
        valid_to: datetime,
        reason: str = "daily",
    ) -> IssuedCredential:
        self._fail_if_injected()
        version, code = self._next_code(room_no)
        self.scheduled.append({"room_no": room_no, "version": version, "reason": reason})
        issued = self._credential(
            room_no=room_no,
            version=version,
            code=code,
            reason=reason,
            status="next",
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self.next_slots[room_no] = self._record(issued, code)
        return issued

    async def activate_next_credential(
        self, *, room_no: str, expected_version: int
    ) -> CredentialMetadata:
        self._fail_if_injected()
        pending = self.next_slots.get(room_no)
        if pending is None or pending["version"] != expected_version:
            raise LockError("设备侧无匹配的 next 凭证", status_code=404)
        self.next_slots.pop(room_no)
        pending["status"] = "active"
        self.current[room_no] = pending
        self.codes[room_no] = pending["code"]
        self.activations.append({"room_no": room_no, "version": expected_version})
        return self._metadata(
            IssuedCredential(
                credential_id=f"devcred-{room_no}-{pending['version']}",
                room_no=room_no,
                code=pending["code"],
                version=pending["version"],
                status="active",
                reason=pending["reason"],
                valid_from=pending["valid_from"],
                valid_to=pending["valid_to"],
            )
        )

    async def revoke_next_credential(
        self, *, room_no: str, expected_version: int | None = None
    ) -> CredentialMetadata:
        self._fail_if_injected()
        pending = self.next_slots.get(room_no)
        if pending is None:
            raise LockError("设备侧无 next 凭证", status_code=404)
        if expected_version is not None and pending["version"] != expected_version:
            raise LockError("凭证版本不匹配", status_code=409)
        self.next_slots.pop(room_no)
        self.next_revocations.append({"room_no": room_no, "expected_version": expected_version})
        return self._metadata(
            IssuedCredential(
                credential_id=f"devcred-{room_no}-{pending['version']}",
                room_no=room_no,
                code=pending["code"],
                version=pending["version"],
                status="next",
                reason=pending["reason"],
                valid_from=pending["valid_from"],
                valid_to=pending["valid_to"],
            ),
            status="revoked",
        )

    async def extend_credential(
        self, *, room_no: str, valid_to: datetime, expected_version: int | None = None
    ) -> CredentialMetadata:
        self._fail_if_injected()
        current = self.current.get(room_no)
        if current is None:
            raise LockError("设备侧无凭证", status_code=404)
        if expected_version is not None and current["version"] != expected_version:
            raise LockError("凭证版本不匹配", status_code=409)
        current["valid_to"] = valid_to
        self.extensions.append(
            {"room_no": room_no, "version": current["version"], "valid_to": valid_to}
        )
        return self._metadata(
            IssuedCredential(
                credential_id=f"devcred-{room_no}-{current['version']}",
                room_no=room_no,
                code=current["code"],
                version=current["version"],
                status="active",
                reason=current["reason"],
                valid_from=current["valid_from"],
                valid_to=valid_to,
            )
        )

    async def revoke_credential(
        self, *, room_no: str, expected_version: int | None = None
    ) -> CredentialMetadata:
        self._guard(room_no, expected_version)
        self.revocations.append({"room_no": room_no, "expected_version": expected_version})
        current = self.current.pop(room_no, None)
        now = datetime.now(HOTEL_TIMEZONE)
        version = current["version"] if current is not None else self.versions.get(room_no, 0)
        return CredentialMetadata(
            credential_id=f"devcred-{room_no}-{version}",
            room_no=room_no,
            version=version,
            status="revoked",
            reason=current["reason"] if current is not None else "manual",
            valid_from=current["valid_from"] if current is not None else now - timedelta(hours=1),
            valid_to=current["valid_to"] if current is not None else now,
        )

    async def list_unlock_records(self, *, room_no: str | None = None) -> list[UnlockRecord]:
        self.unlock_queries.append(room_no)
        return list(self.records)


class _StaticLockClient:
    """把固定替身注入模块级 `LockClient.from_settings()` 调用点。"""

    def __init__(self, client: FakeLockClient) -> None:
        self._client = client

    def from_settings(self) -> FakeLockClient:
        return self._client


def _ensure_checkin_handler_registered() -> None:
    """注册表可能被其他测试重置而模块已被缓存：重载模块重新触发注册，保持顺序无关。"""
    if (
        resolve_handler(
            lock_task_handlers.CHECKIN_TASK_TYPE, lock_task_handlers.CHECKIN_HANDLER_VERSION
        )
        is not None
    ):
        return
    importlib.reload(lock_task_handlers)
    assert (
        resolve_handler(
            lock_task_handlers.CHECKIN_TASK_TYPE, lock_task_handlers.CHECKIN_HANDLER_VERSION
        )
        is not None
    )


def _future(days: int) -> date:
    return datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)


def _events(order: Order, event_type: str) -> list[dict[str, Any]]:
    return [event for event in order.events if event.get("type") == event_type]


async def _guest(session: AsyncSession) -> Guest:
    user = await UserRepository(session).get_or_create(username=f"lock-{uuid4().hex[:8]}")
    return await GuestRepository(session).get_or_create_by_user(user.id)


async def _seed_paid_order(
    session: AsyncSession,
    *,
    guest_id: UUID,
    room_statuses: Sequence[str] = ("available",),
    pay: bool = True,
) -> tuple[Order, list[Room]]:
    """种子订单：房型 + 若干房间 + 订单；pay=True 时经状态机迁移到 paid。"""
    room_type = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8].upper()}",
        name="大床房",
        base_price_fen=39800,
        capacity=2,
        bed_type="king",
    )
    rooms: list[Room] = []
    for index, status in enumerate(room_statuses):
        rooms.append(
            await RoomRepository(session).create(
                room_no=f"L{uuid4().hex[:6].upper()}",
                room_type_id=room_type.id,
                floor=3 + index,
                status=status,
            )
        )
    order = await OrderRepository(session).create(
        order_no=f"H{uuid4().hex[:10].upper()}",
        guest_id=guest_id,
        room_type_id=room_type.id,
        check_in_date=_future(1),
        check_out_date=_future(3),
        nights=2,
        amount_fen=39800 * 2,
    )
    if pay:
        service = OrderService(session)
        await service.transition(order.id, "pending_payment")
        order = await service.transition(order.id, "paid")
    return order, rooms


async def _credential_rows(session: AsyncSession, *, room_no: str) -> list[LockCredential]:
    stmt = (
        select(LockCredential)
        .where(LockCredential.room_no == room_no)
        .order_by(LockCredential.version.asc())
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars())


async def _raw_lock_rows(session: AsyncSession, *, room_no: str) -> list[str]:
    """原始行扫描：绕过 ORM，逐列字符串化后用于明文落库负例。"""
    result = await session.execute(
        text("SELECT * FROM lock_credentials WHERE room_no = :room_no"), {"room_no": room_no}
    )
    return [str(tuple(row)) for row in result]


async def _load_task(session: AsyncSession, task_id: UUID) -> Any:
    task = await TaskRepository(session).get_by_id(task_id)
    assert task is not None
    return task


def test_issue_assigns_room_and_stores_hash_only(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()

        fact = await LockCredentialService(session).issue_for_order(order_id=order.id, client=fake)
        await session.commit()

        reloaded = await OrderRepository(session).get_by_id(order.id)
        assert reloaded is not None
        return {
            "fact": fact,
            "plaintext": fake.codes[room.room_no],
            "order": reloaded,
            "room": room,
            "rows": await _credential_rows(session, room_no=room.room_no),
            "raw": await _raw_lock_rows(session, room_no=room.room_no),
        }

    values = run_with_session(runtime_db_url, _body)
    fact = values["fact"]
    room: Room = values["room"]
    order: Order = values["order"]
    plaintext = values["plaintext"]

    assert fact.replayed is False
    assert fact.room_no == room.room_no
    assert fact.floor == room.floor
    assert fact.code == plaintext  # 明文只随返回载荷流转
    assert values["raw"] and all(plaintext not in raw_row for raw_row in values["raw"])

    assert order.room_id == room.id
    issued_events = _events(order, "credential_issued")
    assert len(issued_events) == 1
    assert issued_events[0]["room_no"] == room.room_no
    assert issued_events[0]["version"] == fact.version == 1
    assert issued_events[0]["reason"] == "checkin"

    (row,) = values["rows"]
    assert row.status == "active"
    assert row.version == 1
    assert row.reason == "checkin"
    assert row.idempotency_key == checkin_idempotency_key(order.id)
    assert row.device_credential_id == fact.device_credential_id == f"devcred-{room.room_no}-1"
    assert row.valid_from.tzinfo is not None
    assert row.valid_to > row.valid_from

    assert plaintext not in row.code_hash
    assert re.fullmatch(r"[0-9a-f]{64}", row.code_hash)
    naked = hashlib.sha256(f"{room.room_no}:1:{plaintext}".encode()).hexdigest()
    assert row.code_hash != naked  # keyed HMAC，不是裸散列


def test_issue_replays_without_second_device_call(runtime_db_url: str) -> None:
    async def _run(factory: SessionFactory) -> dict[str, Any]:
        fake = FakeLockClient()
        async with factory() as session:
            guest = await _guest(session)
            order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            first = await LockCredentialService(session).issue_for_order(
                order_id=order.id, client=fake
            )
            await session.commit()
            order_id, room_no = order.id, room.room_no
        async with factory() as session:
            second = await LockCredentialService(session).issue_for_order(
                order_id=order_id, client=fake
            )
            await session.commit()
            rows = await _credential_rows(session, room_no=room_no)
        return {
            "first": first,
            "second": second,
            "calls": list(fake.issued),
            "row_count": len(rows),
        }

    values = run_with_session_factory(runtime_db_url, _run)
    first, second = values["first"], values["second"]
    assert first.replayed is False
    assert second.replayed is True
    assert second.credential_id == first.credential_id
    assert second.room_no == first.room_no
    assert second.version == first.version == 1
    assert second.code is None  # 重放不重新下发明文
    assert len(values["calls"]) == 1
    assert values["row_count"] == 1


def test_issue_rejects_unpaid_order(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id, pay=False)
        await session.commit()
        fake = FakeLockClient()
        with pytest.raises(ConflictError):
            await LockCredentialService(session).issue_for_order(order_id=order.id, client=fake)
        fresh = await OrderRepository(session).get_by_id(order.id)
        assert fresh is not None
        return {
            "calls": list(fake.issued),
            "row_count": len(await _credential_rows(session, room_no=room.room_no)),
            "room_id": fresh.room_id,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values == {"calls": [], "row_count": 0, "room_id": None}


def test_issue_skips_maintenance_and_conflicting_rooms(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (maintenance_room, taken_room) = await _seed_paid_order(
            session, guest_id=guest.id, room_statuses=("maintenance", "available")
        )
        await OrderRepository(session).create(
            order_no=f"H{uuid4().hex[:10].upper()}",
            guest_id=guest.id,
            room_type_id=order.room_type_id,
            check_in_date=order.check_in_date,
            check_out_date=order.check_out_date,
            nights=order.nights,
            amount_fen=order.amount_fen,
            room_id=taken_room.id,
        )
        await session.commit()
        fake = FakeLockClient()
        service = LockCredentialService(session)

        # 维修房不参与分配；另一间被同日窗未终态订单占用 → 无可分配房间
        with pytest.raises(ConflictError):
            await service.issue_for_order(order_id=order.id, client=fake)
        assert fake.issued == []

        await RoomRepository(session).update_fields(maintenance_room.id, {"status": "available"})
        await session.commit()
        await session.refresh(maintenance_room)
        fact = await service.issue_for_order(order_id=order.id, client=fake)
        await session.commit()
        return {
            "fact": fact,
            "calls": list(fake.issued),
            "maintenance": maintenance_room.room_no,
            "taken": taken_room.room_no,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["fact"].room_no == values["maintenance"]
    assert values["fact"].room_no != values["taken"]
    assert [call["room_no"] for call in values["calls"]] == [values["maintenance"]]


def test_device_failure_rolls_back_allocation(runtime_db_url: str) -> None:
    async def _run(factory: SessionFactory) -> dict[str, Any]:
        fake = FakeLockClient()
        fake.issue_error = "unavailable"
        async with factory() as session:
            guest = await _guest(session)
            order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, room_no = order.id, room.room_no
            with pytest.raises(LockUnavailableError):
                await LockCredentialService(session).issue_for_order(order_id=order_id, client=fake)
            # 不提交：会话退出即整体回滚（与任务处理器失败路径一致）
        async with factory() as session:
            fresh = await OrderRepository(session).get_by_id(order_id)
            return {
                "room_id": fresh.room_id if fresh else None,
                "row_count": len(await _credential_rows(session, room_no=room_no)),
                "calls": list(fake.issued),
            }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values == {"room_id": None, "row_count": 0, "calls": []}


def test_rotate_revokes_previous_and_guards_version(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()
        service = LockCredentialService(session)
        issued = await service.issue_for_order(order_id=order.id, client=fake)
        await session.commit()

        first = await service.rotate(
            room_no=room.room_no, client=fake, expected_version=issued.version
        )
        await session.commit()

        # 旧版本重放改密（契约 §2.3）：本地版本守卫拒绝，设备不被二次调用
        with pytest.raises(ConflictError):
            await service.rotate(room_no=room.room_no, client=fake, expected_version=issued.version)
        assert len(fake.rotations) == 1

        second = await service.rotate(
            room_no=room.room_no, client=fake, expected_version=first.version
        )
        await session.commit()
        return {
            "first": first,
            "second": second,
            "rows": [
                (row.version, row.status)
                for row in await _credential_rows(session, room_no=room.room_no)
            ],
            "rotations": list(fake.rotations),
            "device_version": fake.versions[room.room_no],
            "device_code": fake.codes[room.room_no],
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["first"].version == 2
    assert values["second"].version == 3
    assert values["rows"] == [(1, "revoked"), (2, "revoked"), (3, "active")]
    assert [call["expected_version"] for call in values["rotations"]] == [1, 2]
    assert values["device_version"] == 3
    assert values["second"].code == values["device_code"]


def test_revoke_sets_local_state_and_rejects_stale_version(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()
        service = LockCredentialService(session)
        issued = await service.issue_for_order(order_id=order.id, client=fake)
        await session.commit()

        with pytest.raises(ConflictError):
            await service.revoke(room_no=room.room_no, client=fake, expected_version=99)
        assert fake.revocations == []

        revoked_id = await service.revoke(
            room_no=room.room_no, client=fake, expected_version=issued.version
        )
        await session.commit()

        with pytest.raises(NotFoundError):
            await service.revoke(room_no=room.room_no, client=fake)
        (row,) = await _credential_rows(session, room_no=room.room_no)
        return {
            "revoked_id": revoked_id,
            "issued_id": issued.credential_id,
            "status": row.status,
            "revoked_at": row.revoked_at,
            "room_no": room.room_no,
            "revocations": list(fake.revocations),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["revoked_id"] == values["issued_id"]
    assert values["status"] == "revoked"
    assert values["revoked_at"] is not None
    # 只触达设备一次：版本守卫拒绝与重复吊销均不发请求
    assert values["revocations"] == [{"room_no": values["room_no"], "expected_version": 1}]


def test_list_unlock_records_passes_room_filter(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        fake = FakeLockClient()
        fake.records = [
            UnlockRecord(
                record_id="rec-1",
                room_no="R101",
                source="guest",
                accepted=True,
                at=datetime.now(HOTEL_TIMEZONE),
            )
        ]
        records = await LockCredentialService(session).list_unlock_records(
            client=fake, room_no="R101"
        )
        return {
            "ids": [record.record_id for record in records],
            "queries": list(fake.unlock_queries),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values == {"ids": ["rec-1"], "queries": ["R101"]}


def test_deliver_rotates_on_demand_and_replays_active(runtime_db_url: str) -> None:
    async def _run(factory: SessionFactory) -> dict[str, Any]:
        fake = FakeLockClient()
        async with factory() as session:
            guest = await _guest(session)
            order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, order_no, room_no = order.id, order.order_no, room.room_no
            service = LockCredentialService(session)
            issued = await service.issue_for_order(order_id=order_id, client=fake)
            await session.commit()

        async with factory() as session:
            service = LockCredentialService(session)
            delivered = await service.deliver_for_order(
                guest_id=guest.id, order_no=order_no, client=fake
            )
            await session.commit()
            # 幂等键命中的是签发行（下发后已吊销）：回放取当下 active，而不是键命中行
            replayed = await service.issue_for_order(order_id=order_id, client=fake)
            await session.commit()
            reloaded = await OrderRepository(session).get_by_id(order_id)
            assert reloaded is not None
            return {
                "issued": issued,
                "delivered": delivered,
                "replayed": replayed,
                "order": reloaded,
                "rows": [
                    (row.version, row.status, row.reason)
                    for row in await _credential_rows(session, room_no=room_no)
                ],
                "raw": await _raw_lock_rows(session, room_no=room_no),
                "rotations": list(fake.rotations),
                "device_code": fake.codes[room_no],
            }

    values = run_with_session_factory(runtime_db_url, _run)
    delivered = values["delivered"]
    assert delivered.reason == "delivery"
    assert delivered.version == 2
    assert delivered.code == values["device_code"]
    assert delivered.code != values["issued"].code  # 下发即换新码
    assert values["rotations"] == [
        {"room_no": delivered.room_no, "version": 2, "expected_version": 1}
    ]

    # 旧码只活在签发返回载荷与设备侧；两代明文都不落库（A2）
    for plaintext in (values["issued"].code, delivered.code):
        assert all(plaintext not in raw_row for raw_row in values["raw"])
    assert values["rows"] == [(1, "revoked", "checkin"), (2, "active", "delivery")]

    replayed = values["replayed"]
    assert replayed.replayed is True
    assert replayed.version == 2  # 回放当下 active 版本
    assert replayed.code is None

    order: Order = values["order"]
    assert order.room_id == delivered.room_id
    delivered_events = _events(order, "credential_delivered")
    assert len(delivered_events) == 1
    assert delivered_events[0] == {
        "type": "credential_delivered",
        "room_no": delivered.room_no,
        "version": 2,
        "reason": "delivery",
    }


def test_deliver_rejects_unpaid_and_foreign_orders(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        unpaid, _ = await _seed_paid_order(session, guest_id=guest.id, pay=False)
        other = await _guest(session)
        fake = FakeLockClient()
        service = LockCredentialService(session)

        with pytest.raises(NotFoundError):
            await service.deliver_for_order(guest_id=other.id, order_no=order.order_no, client=fake)
        with pytest.raises(ConflictError):
            await service.deliver_for_order(
                guest_id=guest.id, order_no=unpaid.order_no, client=fake
            )
        # 凭证未就绪（未签发）按可重试口径拒绝，不调用设备
        with pytest.raises(ConflictError):
            await service.deliver_for_order(guest_id=guest.id, order_no=order.order_no, client=fake)
        await session.commit()
        return {
            "rotations": list(fake.rotations),
            "row_count": len(await _credential_rows(session, room_no=room.room_no)),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values == {"rotations": [], "row_count": 0}


def test_overview_reports_room_and_window_without_plaintext(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        guest = await _guest(session)
        order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
        fake = FakeLockClient()
        service = LockCredentialService(session)
        before = await service.overview_for_order(guest_id=guest.id, order_no=order.order_no)

        issued = await service.issue_for_order(order_id=order.id, client=fake)
        await session.commit()
        after = await service.overview_for_order(guest_id=guest.id, order_no=order.order_no)
        await session.commit()
        return {"before": before, "after": after, "issued": issued, "room": room}

    values = run_with_session(runtime_db_url, _body)
    before = values["before"]
    assert before.room_no is None and before.floor is None and before.credential is None

    after = values["after"]
    room: Room = values["room"]
    assert after.room_no == room.room_no
    assert after.floor == room.floor
    assert after.credential is not None
    assert after.credential.version == values["issued"].version
    assert after.credential.code is None  # 只读视图永不携带明文


def test_checkin_task_end_to_end(runtime_db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLockClient()
    _ensure_checkin_handler_registered()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(fake))

    async def _run(factory: SessionFactory) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, room_no, floor = order.id, room.room_no, room.floor

        submission = await submit_task(
            task_type=lock_task_handlers.CHECKIN_TASK_TYPE,
            handler_version=lock_task_handlers.CHECKIN_HANDLER_VERSION,
            payload={"order_id": str(order_id), "payment_id": str(uuid4())},
            dedupe_key=f"checkin.prepare:{order_id}",
            session_factory=factory,
        )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)

        async with factory() as session:
            task = await _load_task(session, submission.task_id)
            order = await OrderRepository(session).get_by_id(order_id)
            assert order is not None
            return {
                "outcome": outcome,
                "status": task.status,
                "result": task.result,
                "order": order,
                "rows": await _credential_rows(session, room_no=room_no),
                "raw": await _raw_lock_rows(session, room_no=room_no),
                "room_no": room_no,
                "floor": floor,
            }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    assert values["status"] == "succeeded"

    result = values["result"]
    assert set(result) == {
        "order_id",
        "room_no",
        "floor",
        "credential_version",
        "valid_from",
        "valid_to",
        "replayed",
        "notify_status",
    }
    assert result["room_no"] == values["room_no"]
    assert result["floor"] == values["floor"]
    assert result["credential_version"] == 1
    assert result["replayed"] is False
    # 未配置订阅消息模板：通知跳过并留痕，不影响任务结果（本节只用门锁替身，不发渠道请求）
    assert result["notify_status"] == "skipped"

    order: Order = values["order"]
    assert order.status == "paid"
    assert order.room_id is not None
    assert len(_events(order, "credential_issued")) == 1
    (notified,) = _events(order, "credential_notified")
    assert notified["status"] == "skipped"
    assert notified["reason"] == "未配置订阅消息模板"

    (row,) = values["rows"]
    assert row.status == "active"
    assert row.version == 1

    plaintext = fake.codes[values["room_no"]]
    assert plaintext not in str(result)  # 任务结果随任务落库：禁止携带明文
    assert all(plaintext not in raw_row for raw_row in values["raw"])


def test_checkin_task_failure_keeps_order_paid_and_records_blocked(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLockClient()
    fake.issue_error = "unavailable"
    _ensure_checkin_handler_registered()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(fake))

    async def _run(factory: SessionFactory) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session)
            order, (room,) = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, room_no = order.id, room.room_no

        submission = await submit_task(
            task_type=lock_task_handlers.CHECKIN_TASK_TYPE,
            handler_version=lock_task_handlers.CHECKIN_HANDLER_VERSION,
            payload={"order_id": str(order_id), "payment_id": str(uuid4())},
            dedupe_key=f"checkin.prepare:{order_id}",
            session_factory=factory,
        )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)

        async with factory() as session:
            task = await _load_task(session, submission.task_id)
            order = await OrderRepository(session).get_by_id(order_id)
            assert order is not None
            return {
                "outcome": outcome,
                "status": task.status,
                "error": task.error,
                "order": order,
                "rows": await _credential_rows(session, room_no=room_no),
            }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "failed"
    assert values["status"] == "failed"
    assert "门锁服务不可达" in str(values["error"])

    # 失败关闭负例：注入门锁失败 → 订单不得进入 completed，也不得残留分配
    order: Order = values["order"]
    assert order.status == "paid"
    assert order.room_id is None
    blocked = _events(order, "checkin_blocked")
    assert len(blocked) == 1
    assert "门锁服务不可达" in str(blocked[0]["reason"])
    assert values["rows"] == []
