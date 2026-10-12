"""P5 客房服务与保洁派单集成测试（真实 PostgreSQL + 可选 TestClient）。

覆盖：机器人配送创建/查询/状态迁移、呼叫清洁任务创建、保洁任务完整生命周期
（accept→start→finish→verify→close）、退房与改密验证链路、投诉提交。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.main import app
from app.models.domain import (
    HousekeepingTask,
    Order,
)
from app.repositories import (
    GuestRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    StaffRepository,
    UserRepository,
)
from app.security import hash_password
from app.services.checkout_service import CheckoutService
from app.services.complaint_service import ComplaintService
from app.services.housekeeping_service import HousekeepingService
from app.services.order_service import OrderService
from app.services.robot_delivery_service import RobotDeliveryService
from tests.integration.test_lock_credentials import FakeLockClient

pytestmark = pytest.mark.integration


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
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _run(api: Api, body: Any) -> Any:
    async def _inner() -> Any:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            result = await body(session)
            await session.commit()
            return result

    return asyncio.run(_inner())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _day(offset: int) -> str:
    return (datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=offset)).isoformat()


async def _seed_guest_user(
    session: AsyncSession, *, username: str = "test-guest"
) -> tuple[UUID, UUID]:
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create(username=username)
    guest_repo = GuestRepository(session)
    guest = await guest_repo.get_or_create_by_user(user.id)
    return user.id, guest.id


async def _seed_staff_user(
    session: AsyncSession, *, staff_no: str = "test-staff", name: str = "测试保洁"
) -> UUID:
    user_repo = UserRepository(session)
    user = await user_repo.get_or_create(username=staff_no, display_name=name)
    staff_repo = StaffRepository(session)
    existing = await staff_repo.get_by_staff_no(staff_no)
    if existing is None:
        await staff_repo.create(staff_no=staff_no, name=name, user_id=user.id)
    else:
        if existing.user_id != user.id:
            await staff_repo.update_fields(existing.id, {"user_id": user.id})
    return user.id


async def _seed_room_and_order(
    session: AsyncSession, *, guest_id: UUID, status: str = "checked_in"
) -> tuple[UUID, str, UUID]:
    room_type_repo = RoomTypeRepository(session)
    room_types = await room_type_repo.list_all()
    room_type = room_types[0] if room_types else None
    if room_type is None:
        room_type = await room_type_repo.create(
            code="TEST",
            name="测试房型",
            base_price_fen=10000,
            capacity=2,
            bed_type="king",
        )
    room_repo = RoomRepository(session)
    rooms = await room_repo.list_all()
    room = rooms[0] if rooms else None
    if room is None:
        room = await room_repo.create(room_no="9999", room_type_id=room_type.id, floor=9)

    order_service = OrderService(session)
    order = await order_service.create(
        order_no=f"P5TEST-{uuid4().hex[:8].upper()}",
        guest_id=guest_id,
        room_type_id=room_type.id,
        check_in_date=datetime.now(HOTEL_TIMEZONE).date(),
        check_out_date=datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=2),
        amount_fen=20000,
        room_id=room.id,
    )
    if status != "created":
        for s in ORDER_TRANSITIONS_TO[status]:
            order = await order_service.transition(order.id, s)
    return order.id, room.room_no, room.id


# 从 created 到达目标状态的最短路径
ORDER_TRANSITIONS_TO: dict[str, tuple[str, ...]] = {
    "pending_payment": ("pending_payment",),
    "paid": ("pending_payment", "paid"),
    "checked_in": ("pending_payment", "paid", "checked_in"),
    "checkout_pending": ("pending_payment", "paid", "checked_in", "checkout_pending"),
    "completed": ("pending_payment", "paid", "checked_in", "checkout_pending", "completed"),
}


class TestRobotDelivery:
    def test_create_and_list(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            service = RobotDeliveryService(session)
            d1 = await service.create(
                guest_id=guest_id, room_no="301",
                item={"name": "矿泉水", "quantity": 2},
            )
            d2 = await service.create(
                guest_id=guest_id, room_no="302",
                item={"name": "拖鞋", "quantity": 1},
            )
            assert d1.status == "queued"
            assert d2.status == "queued"
            deliveries = await service.list_for_guest(guest_id)
            assert len(deliveries) == 2

        _run(api, body)

    def test_idempotency_replay(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            service = RobotDeliveryService(session)
            d1 = await service.create(
                guest_id=guest_id, room_no="301",
                item={"name": "毛巾", "quantity": 1},
                idempotency_key="dup-key-1",
            )
            d2 = await service.create(
                guest_id=guest_id, room_no="301",
                item={"name": "毛巾", "quantity": 1},
                idempotency_key="dup-key-1",
            )
            assert d1.id == d2.id  # 重放返回既有
            assert d2.status == "queued"

        _run(api, body)

    def test_transition(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            service = RobotDeliveryService(session)
            d = await service.create(
                guest_id=guest_id, room_no="301",
                item={"name": "可乐", "quantity": 1},
            )
            for status in ("dispatching", "delivering", "arrived", "delivered"):
                d = await service.transition(d.id, status)
            assert d.status == "delivered"
            assert d.finished_at is not None

        _run(api, body)


class TestHousekeepingTask:
    def test_call_clean_create(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id
            )
            hs = HousekeepingService(session)
            task = await hs.create(
                task_type="call_clean", room_id=room_id, room_no=room_no,
                payload={"note": "需要打扫"},
            )
            assert task.task_type == "call_clean"
            assert task.status == "pending"

        _run(api, body)

    def test_task_full_lifecycle(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id
            )
            staff_user_id = await _seed_staff_user(session)
            staff_repo = StaffRepository(session)
            staff = await staff_repo.get_by_user_id(staff_user_id)
            assert staff is not None

            hs = HousekeepingService(session)
            task = await hs.create(
                task_type="call_clean", room_id=room_id, room_no=room_no,
            )

            task = await hs.transition(task.id, "accepted", assigned_staff_id=staff.id)
            assert task.status == "accepted"
            assert task.assigned_staff_id == staff.id

            task = await hs.transition(task.id, "in_progress")
            assert task.status == "in_progress"

            task = await hs.transition(task.id, "done")
            assert task.status == "done"

            task = await hs.transition(task.id, "verified")
            assert task.status == "verified"

            task = await hs.transition(task.id, "closed")
            assert task.status == "closed"

        _run(api, body)

    def test_checkout_clean_verify_requires_rotation(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id, status="checkout_pending"
            )
            staff_user_id = await _seed_staff_user(session)
            staff_repo = StaffRepository(session)
            staff = await staff_repo.get_by_user_id(staff_user_id)
            assert staff is not None

            hs = HousekeepingService(session)
            task = await hs.create(
                task_type="checkout_clean", room_id=room_id, room_no=room_no,
                order_id=order_id,
            )
            task = await hs.transition(task.id, "accepted", assigned_staff_id=staff.id)
            task = await hs.transition(task.id, "in_progress")
            task = await hs.transition(task.id, "done")

            # 不带 password_rotated 的 verify 必须拒绝
            from app.domain.errors import ConflictError
            with pytest.raises(ConflictError, match="确认密码已修改"):
                await hs.transition(task.id, "verified")

            # 带 password_rotated=true 的 verify 必须通过
            task = await hs.transition(
                task.id, "verified", evidence={"password_rotated": True}
            )
            assert task.status == "verified"

        _run(api, body)


class TestCheckoutFlow:
    def test_initiate_checkout_creates_clean_task(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id, status="checked_in"
            )

            checkout = CheckoutService(session)
            _order = await session.get(Order, order_id)
            assert _order is not None
            order = await checkout.initiate_checkout(
                guest_id=guest_id, order_no=_order.order_no
            )
            assert order.status == "checkout_pending"

            # 验证任务已创建
            tasks = list(
                (await session.execute(
                    select(HousekeepingTask).where(HousekeepingTask.order_id == order_id)
                )).scalars()
            )
            assert len(tasks) >= 1
            assert tasks[0].task_type == "checkout_clean"

        _run(api, body)

    def test_verify_and_complete_revokes_credential(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id, status="checked_in"
            )
            order_repo = OrderRepository(session)
            order = await order_repo.get_by_id(order_id)
            assert order is not None

            checkout = CheckoutService(session)
            order = await checkout.initiate_checkout(
                guest_id=guest_id, order_no=order.order_no
            )

            tasks = list(
                (await session.execute(
                    select(HousekeepingTask).where(HousekeepingTask.order_id == order_id)
                )).scalars()
            )
            task = tasks[0]

            hs = HousekeepingService(session)
            task = await hs.transition(task.id, "accepted")
            task = await hs.transition(task.id, "in_progress")
            task = await hs.transition(task.id, "done")
            task = await hs.transition(task.id, "verified", evidence={"password_rotated": True})

            # verify_and_complete 吊销凭证并完成订单
            lock_client = FakeLockClient()
            result = await checkout.verify_and_complete(
                task_id=task.id, lock_client=lock_client  # type: ignore[arg-type]
            )
            assert result is not None
            assert result.status == "completed"

        _run(api, body)


class TestGuestAPI:
    def _customer_token(self, api: Api) -> str:
        """创建一个客人账号并登录，返回会话令牌。"""
        password = "test-pass-123"
        username = f"p5guest-{uuid4().hex[:8]}"

        async def _body(session: AsyncSession) -> None:
            await UserRepository(session).create_account(
                username=username, password_hash=hash_password(password), role="customer"
            )

        _run(api, _body)
        resp = api.client.post("/api/auth/login", json={"username": username, "password": password})
        assert resp.status_code == 200, resp.text
        return str(resp.json()["token"])

    def test_request_clean_via_api(self, api: Api) -> None:
        token = self._customer_token(api)

        async def body(session: AsyncSession) -> str:
            _, guest_id = await _seed_guest_user(session, username=uuid4().hex[:8])
            _, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest_id
            )
            return room_no

        room_no = _run(api, body)

        resp = api.client.post(
            "/api/guest/services/clean",
            json={"room_no": room_no, "note": "请打扫一下"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["task_type"] == "call_clean"
        assert data["status"] == "pending"

    def test_request_delivery_via_api(self, api: Api) -> None:
        token = self._customer_token(api)

        resp = api.client.post(
            "/api/guest/services/deliveries",
            json={"item_name": "矿泉水", "item_quantity": 2, "room_no": "301"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "queued"
        assert data["item"]["name"] == "矿泉水"

    def test_submit_complaint_via_api(self, api: Api) -> None:
        token = self._customer_token(api)

        resp = api.client.post(
            "/api/guest/services/complaints",
            json={"content": "房间太吵了", "category": "noise", "room_no": "301"},
            headers=_auth(token),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "open"

    def test_checkout_via_api(self, api: Api) -> None:
        password = "test-pass-123"
        username = f"p5co-{uuid4().hex[:8]}"

        async def _seed(session: AsyncSession) -> tuple[str, str]:
            user = await UserRepository(session).create_account(
                username=username, password_hash=hash_password(password), role="customer"
            )
            guest = await GuestRepository(session).get_or_create_by_user(user.id)
            order_id, room_no, room_id = await _seed_room_and_order(
                session, guest_id=guest.id, status="checked_in"
            )
            order_repo = OrderRepository(session)
            order = await order_repo.get_by_id(order_id)
            assert order is not None
            return order.order_no, room_no

        order_no, _ = _run(api, _seed)
        resp = api.client.post(
            "/api/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200
        token = str(resp.json()["token"])

        resp = api.client.post(
            f"/api/guest/services/orders/{order_no}/checkout",
            headers=_auth(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "checkout_pending"

    def test_unauthorized_returns_401(self, api: Api) -> None:
        resp = api.client.post(
            "/api/guest/services/clean", json={"room_no": "301"}
        )
        assert resp.status_code == 401

        resp = api.client.post(
            "/api/guest/services/deliveries", json={"item_name": "水"}
        )
        assert resp.status_code == 401


class TestComplaintService:
    def test_create_complaint(self, api: Api) -> None:
        async def body(session: AsyncSession) -> None:
            _, guest_id = await _seed_guest_user(session)
            service = ComplaintService(session)
            c = await service.create(
                guest_id=guest_id,
                content="噪音太大",
                category="noise",
                room_no="301",
            )
            assert c.status == "open"
            assert c.category == "noise"

        _run(api, body)