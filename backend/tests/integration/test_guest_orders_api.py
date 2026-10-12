"""P4-T8a C 端房型与订单 REST 集成测试（真实 PostgreSQL + TestClient）。

覆盖：房型列表/详情与房态口径（库存 = 房型房间数 vs 同日期窗未终态重叠订单数）、日期窗校验、
下单与幂等重放（键按客人命名空间化：他人用同一客户端键各建各单）、我的订单只含本人、
详情归属按「不存在」口径 404、改期/续订/取消的状态与金额事实、续订保持凭证只展期、
门锁不可达时续订失败关闭（订单不变）、发票一单一张与回放 200、未认证 401（A6 取证）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock.service import LockCredentialService
from app.main import app
from app.repositories import (
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.security import hash_password
from app.services import booking_service
from app.services.order_service import OrderService
from tests.integration.test_lock_credentials import FakeLockClient, _StaticLockClient

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


def _moment(value: str) -> datetime:
    """统一时区口径后比较时刻：接口回 UTC(`Z`)，替身与库内为 +08:00。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seed_customer(api: Api, *, password: str = "guest-pass-123") -> str:
    username = f"guest-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> None:
        await UserRepository(session).create_account(
            username=username, password_hash=hash_password(password), role="customer"
        )

    _run(api, _body)
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _seed_room_type(
    api: Api, *, rooms: int = 2, price_fen: int = 39800, active: bool = True
) -> str:
    code = f"RT-{uuid4().hex[:6].upper()}"

    async def _body(session: AsyncSession) -> None:
        room_type = await RoomTypeRepository(session).create(
            code=code,
            name="测试大床房",
            base_price_fen=price_fen,
            capacity=2,
            bed_type="king",
            amenities=["wifi"],
            active=active,
        )
        for index in range(rooms):
            await RoomRepository(session).create(
                room_no=f"A{uuid4().hex[:6].upper()}",
                room_type_id=room_type.id,
                floor=3 + index,
            )

    _run(api, _body)
    return code


def _seed_blocking_order(api: Api, *, room_type_code: str, guest_token: str) -> str:
    """占位订单：同日期窗的未终态订单，用于验证房态口径与订满拒绝。"""
    response = api.client.post(
        "/api/guest/orders",
        headers=_auth(guest_token),
        json={
            "room_type_code": room_type_code,
            "check_in_date": _day(1),
            "check_out_date": _day(3),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["order_no"])


def _pay_and_assign(
    api: Api, *, order_no: str, fake: FakeLockClient | None = None
) -> dict[str, Any]:
    """把订单推进到 paid 并分配房间；给出 fake 时同时签发入住凭证（真实服务层路径）。"""

    async def _body(session: AsyncSession) -> dict[str, Any]:
        orders = OrderRepository(session)
        order = await orders.get_by_order_no(order_no)
        assert order is not None
        service = OrderService(session)
        await service.transition(order.id, "pending_payment")
        order = await service.transition(order.id, "paid")
        room_type = await RoomTypeRepository(session).get_by_id(order.room_type_id)
        assert room_type is not None
        rooms = await RoomRepository(session).list_all(room_type_id=room_type.id)
        assert rooms
        await orders.apply_fields(
            order.id, from_statuses=(order.status,), values={"room_id": rooms[0].id}
        )
        fact = None
        if fake is not None:
            fact = await LockCredentialService(session).issue_for_order(
                order_id=order.id, client=fake
            )
        refreshed = await orders.get_by_order_no(order_no)
        assert refreshed is not None
        return {
            "room_no": rooms[0].room_no,
            "credential_version": fact.version if fact is not None else None,
            "valid_to": fact.valid_to.isoformat() if fact is not None else None,
            "check_out": refreshed.check_out_date.isoformat(),
        }

    return _run(api, _body)


def test_room_type_list_and_availability_window(api: Api) -> None:
    token = _seed_customer(api)
    code = _seed_room_type(api, rooms=2, price_fen=41800)
    _seed_room_type(api, rooms=1, active=False)
    blocker = _seed_customer(api)
    _seed_blocking_order(api, room_type_code=code, guest_token=blocker)

    listing = api.client.get("/api/guest/room-types", headers=_auth(token))
    assert listing.status_code == 200, listing.text
    rows = listing.json()
    assert all(row["availability"] is None for row in rows)  # 不带日期窗只给房型事实
    assert all(row["code"] != "RT-INACTIVE" for row in rows)
    assert not any(row["active"] is False for row in rows if "active" in row)

    windowed = api.client.get(
        "/api/guest/room-types",
        headers=_auth(token),
        params={"check_in_date": _day(1), "check_out_date": _day(3)},
    )
    assert windowed.status_code == 200, windowed.text
    offer = next(row for row in windowed.json() if row["code"] == code)
    availability = offer["availability"]
    assert availability["nights"] == 2
    assert availability["total_rooms"] == 2
    assert availability["booked_rooms"] == 1
    assert availability["available_rooms"] == 1
    assert availability["amount_fen"] == 41800 * 2

    free_window = api.client.get(
        f"/api/guest/room-types/{code}",
        headers=_auth(token),
        params={"check_in_date": _day(10), "check_out_date": _day(12)},
    )
    assert free_window.status_code == 200, free_window.text
    assert free_window.json()["availability"]["available_rooms"] == 2


def test_room_type_detail_negative_cases(api: Api) -> None:
    token = _seed_customer(api)
    code = _seed_room_type(api, active=False)

    assert api.client.get(f"/api/guest/room-types/{code}", headers=_auth(token)).status_code == 404
    assert api.client.get("/api/guest/room-types/NOPE", headers=_auth(token)).status_code == 404
    assert api.client.get("/api/guest/room-types").status_code == 401

    bad_window = api.client.get(
        "/api/guest/room-types",
        headers=_auth(token),
        params={"check_in_date": _day(3), "check_out_date": _day(1)},
    )
    assert bad_window.status_code == 422
    half_window = api.client.get(
        "/api/guest/room-types", headers=_auth(token), params={"check_in_date": _day(1)}
    )
    assert half_window.status_code == 422
    past_window = api.client.get(
        "/api/guest/room-types",
        headers=_auth(token),
        params={"check_in_date": _day(-2), "check_out_date": _day(-1)},
    )
    assert past_window.status_code == 422


def test_create_order_replays_per_guest_and_never_leaks_others(api: Api) -> None:
    code = _seed_room_type(api, rooms=3)
    guest_a = _seed_customer(api)
    guest_b = _seed_customer(api)
    body = {
        "room_type_code": code,
        "check_in_date": _day(1),
        "check_out_date": _day(3),
        "guest_count": 2,
        "idempotency_key": "submit-1",
    }

    first = api.client.post("/api/guest/orders", headers=_auth(guest_a), json=body)
    assert first.status_code == 201, first.text
    replay = api.client.post("/api/guest/orders", headers=_auth(guest_a), json=body)
    assert replay.status_code == 200  # 重放：同一订单，不产生第二张单
    assert replay.json()["order_no"] == first.json()["order_no"]

    # 同一客户端键在另一个客人手里是另一命名空间：各建各单
    other = api.client.post("/api/guest/orders", headers=_auth(guest_b), json=body)
    assert other.status_code == 201, other.text
    assert other.json()["order_no"] != first.json()["order_no"]

    created = first.json()
    assert created["status"] == "created"
    assert created["nights"] == 2
    assert created["amount_fen"] == 39800 * 2
    assert created["room_type_code"] == code
    assert created["credential"] is None
    assert created["invoice"] is None

    listing = api.client.get("/api/guest/orders", headers=_auth(guest_a))
    assert listing.status_code == 200
    assert [row["order_no"] for row in listing.json()] == [created["order_no"]]


def test_create_order_validates_window_capacity_and_inventory(api: Api) -> None:
    token = _seed_customer(api)
    code = _seed_room_type(api, rooms=1)

    unknown = api.client.post(
        "/api/guest/orders",
        headers=_auth(token),
        json={"room_type_code": "NOPE", "check_in_date": _day(1), "check_out_date": _day(2)},
    )
    assert unknown.status_code == 422

    capacity = api.client.post(
        "/api/guest/orders",
        headers=_auth(token),
        json={
            "room_type_code": code,
            "check_in_date": _day(1),
            "check_out_date": _day(2),
            "guest_count": 9,
        },
    )
    assert capacity.status_code == 422

    first = api.client.post(
        "/api/guest/orders",
        headers=_auth(token),
        json={"room_type_code": code, "check_in_date": _day(1), "check_out_date": _day(2)},
    )
    assert first.status_code == 201, first.text
    sold_out = api.client.post(
        "/api/guest/orders",
        headers=_auth(token),
        json={"room_type_code": code, "check_in_date": _day(1), "check_out_date": _day(2)},
    )
    assert sold_out.status_code == 409  # 唯一一间房已被本日期窗占用


def test_order_detail_ownership_uses_not_found(api: Api) -> None:
    code = _seed_room_type(api)
    owner = _seed_customer(api)
    stranger = _seed_customer(api)
    order_no = _seed_blocking_order(api, room_type_code=code, guest_token=owner)

    mine = api.client.get(f"/api/guest/orders/{order_no}", headers=_auth(owner))
    assert mine.status_code == 200, mine.text
    assert mine.json()["order_no"] == order_no

    theirs = api.client.get(f"/api/guest/orders/{order_no}", headers=_auth(stranger))
    assert theirs.status_code == 404  # 不区分「不存在」，避免目录探测
    assert api.client.get("/api/guest/orders/H000000", headers=_auth(owner)).status_code == 404
    assert api.client.get(f"/api/guest/orders/{order_no}").status_code == 401


def test_reschedule_cancel_and_terminal_guard(api: Api) -> None:
    code = _seed_room_type(api)
    token = _seed_customer(api)
    order_no = _seed_blocking_order(api, room_type_code=code, guest_token=token)

    moved = api.client.post(
        f"/api/guest/orders/{order_no}/reschedule",
        headers=_auth(token),
        json={"check_in_date": _day(4), "check_out_date": _day(7)},
    )
    assert moved.status_code == 200, moved.text
    detail = moved.json()
    assert detail["nights"] == 3
    assert detail["amount_fen"] == 39800 * 3
    assert detail["check_in_date"] == _day(4)
    assert [event["type"] for event in detail["events"]] == ["rescheduled"]

    cancelled = api.client.post(
        f"/api/guest/orders/{order_no}/cancel", headers=_auth(token), json={"reason": "行程变更"}
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_reason"] == "行程变更"
    assert cancelled.json()["cancelled_at"] is not None

    again = api.client.post(f"/api/guest/orders/{order_no}/cancel", headers=_auth(token), json={})
    assert again.status_code == 409  # 终态不可再迁移
    reschedule_after = api.client.post(
        f"/api/guest/orders/{order_no}/reschedule",
        headers=_auth(token),
        json={"check_in_date": _day(5), "check_out_date": _day(6)},
    )
    assert reschedule_after.status_code == 409


def test_renew_keeps_credential_and_extends_window(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _seed_room_type(api)
    token = _seed_customer(api)
    order_no = _seed_blocking_order(api, room_type_code=code, guest_token=token)
    fake = FakeLockClient()
    seeded = _pay_and_assign(api, order_no=order_no, fake=fake)
    assert seeded["credential_version"] == 1
    # 凭证由替身签发，展期也必须落在同一替身（否则打到真实设备侧的"无此凭证"）
    monkeypatch.setattr(booking_service, "LockClient", _StaticLockClient(fake))

    renewed = api.client.post(
        f"/api/guest/orders/{order_no}/renew",
        headers=_auth(token),
        json={"check_out_date": _day(6)},
    )
    assert renewed.status_code == 200, renewed.text
    detail = renewed.json()
    assert detail["status"] == "paid"
    assert detail["check_out_date"] == _day(6)
    assert detail["nights"] == 5
    assert detail["room_no"] == seeded["room_no"]
    assert [event["type"] for event in detail["events"]] == [
        "credential_issued",
        "renewed",
        "credential_extended",
    ]

    # 续订保持当前凭证：版本不变、只把窗口终点展到新退房日期
    assert detail["credential"]["version"] == 1
    assert _moment(detail["credential"]["valid_to"]) > _moment(seeded["valid_to"])
    assert len(fake.extensions) == 1
    assert fake.rotations == [] and len(fake.issued) == 1

    replay = api.client.post(
        f"/api/guest/orders/{order_no}/renew",
        headers=_auth(token),
        json={"check_out_date": _day(6)},
    )
    assert replay.status_code == 422  # 新退房日期必须晚于当前退房日期
    assert len(fake.extensions) == 1


def test_renew_fails_closed_when_lock_unavailable(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    code = _seed_room_type(api)
    token = _seed_customer(api)
    order_no = _seed_blocking_order(api, room_type_code=code, guest_token=token)
    fake = FakeLockClient()
    seeded = _pay_and_assign(api, order_no=order_no, fake=fake)

    class _DownLockClient:
        @classmethod
        def from_settings(cls) -> FakeLockClient:
            down = FakeLockClient()
            down.issue_error = "unavailable"
            return down

    monkeypatch.setattr(booking_service, "LockClient", _DownLockClient)
    response = api.client.post(
        f"/api/guest/orders/{order_no}/renew",
        headers=_auth(token),
        json={"check_out_date": _day(6)},
    )
    assert response.status_code == 409, response.text
    assert "续订未生效" in response.json()["detail"]

    after = api.client.get(f"/api/guest/orders/{order_no}", headers=_auth(token)).json()
    assert after["check_out_date"] == seeded["check_out"]  # 订单事实未被改写
    assert _moment(after["credential"]["valid_to"]) == _moment(seeded["valid_to"])
    assert [event["type"] for event in after["events"]] == ["credential_issued"]


def test_invoice_is_one_per_order_with_replay(api: Api) -> None:
    code = _seed_room_type(api)
    token = _seed_customer(api)
    stranger = _seed_customer(api)
    order_no = _seed_blocking_order(api, room_type_code=code, guest_token=token)

    unpaid = api.client.post(
        f"/api/guest/orders/{order_no}/invoice", headers=_auth(token), json={"title": "个人"}
    )
    assert unpaid.status_code == 409  # 未支付不可开票

    _pay_and_assign(api, order_no=order_no)
    created = api.client.post(
        f"/api/guest/orders/{order_no}/invoice",
        headers=_auth(token),
        json={"title": "皓晨科技有限公司", "tax_no": "91330100MA27XW8H1F"},
    )
    assert created.status_code == 201, created.text
    invoice = created.json()
    assert invoice["status"] == "requested"
    assert invoice["amount_fen"] == 39800 * 2

    replay = api.client.post(
        f"/api/guest/orders/{order_no}/invoice", headers=_auth(token), json={"title": "另一个抬头"}
    )
    assert replay.status_code == 200  # 回放既有发票，不新建第二张
    assert replay.json()["id"] == invoice["id"]
    assert replay.json()["title"] == invoice["title"]

    detail = api.client.get(f"/api/guest/orders/{order_no}", headers=_auth(token)).json()
    assert detail["invoice"]["id"] == invoice["id"]

    assert (
        api.client.post(
            f"/api/guest/orders/{order_no}/invoice", headers=_auth(stranger), json={"title": "x"}
        ).status_code
        == 403
    )


def test_guest_endpoints_require_authentication(api: Api) -> None:
    code = _seed_room_type(api)
    assert api.client.get("/api/guest/orders").status_code == 401
    assert api.client.post("/api/guest/orders", json={"room_type_code": code}).status_code == 401
    assert api.client.get("/api/guest/room-types").status_code == 401
    assert api.client.post("/api/guest/orders/H1/cancel", json={}).status_code == 401
