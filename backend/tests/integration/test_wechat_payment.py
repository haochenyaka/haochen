"""P4-T2 微信登录与支付集成测试（真实 PostgreSQL + Redis 会话）：

覆盖：code2session 登录幂等建立身份与客人档案；JSAPI 下单的归属/状态/渠道失败边界；
回调验签负例（不写事实）；重复回调只产生一单一条任务（A3）；金额不符拒绝记账；
乱序失败回调不回退已 paid 状态。签名按契约 §1.5 在测试侧独立计算，不复用被测实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import get_session
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.main import app
from app.models.domain import Guest, Order, Payment
from app.models.runtime import User
from app.models.task import Task
from app.repositories import (
    GuestRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
)
from app.routers import guest_auth_router, wechat_webhook_router
from app.services.order_service import OrderService
from app.wechat import PrepayOrder, WechatError, WechatSession, WechatUnavailableError
from tests.integration.db_utils import run_with_session

pytestmark = pytest.mark.integration

CALLBACK_URL = "/api/webhooks/wechat/pay"
AMOUNT_FEN = 79600


class FakeWechatClient:
    """渠道替身：openid 派生与下单形状与契约 mock 一致（§4.1/§4.2）。"""

    def __init__(self) -> None:
        self.login_error: str | None = None  # None | "invalid" | "unavailable"
        self.pay_error: str | None = None  # None | "rejected" | "unavailable"
        self.pay_orders: list[dict[str, Any]] = []

    async def code2session(self, code: str) -> WechatSession:
        if self.login_error == "unavailable":
            raise WechatUnavailableError("fake channel down")
        if self.login_error == "invalid":
            raise WechatError("fake invalid code")
        digest = hashlib.sha256(code.encode()).hexdigest()[:16]
        return WechatSession(openid=f"openid_{digest}", session_key="fake-session-key")

    async def create_pay_order(
        self, *, order_no: str, amount_fen: int, openid: str, description: str = "房费"
    ) -> PrepayOrder:
        if self.pay_error == "unavailable":
            raise WechatUnavailableError("fake channel down")
        if self.pay_error == "rejected":
            raise WechatError("fake pay rejected")
        prepay_id = f"prepay_{uuid4().hex[:12]}"
        self.pay_orders.append({"order_no": order_no, "amount_fen": amount_fen, "openid": openid})
        return PrepayOrder(
            prepay_id=prepay_id,
            pay_params={
                "appId": "wx-simulated",
                "package": f"prepay_id={prepay_id}",
                "signType": "RSA",
            },
        )

    async def aclose(self) -> None:
        return None


class FakeTaskDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_task(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def aclose(self) -> None:
        return None


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine
    db_url: str
    wechat: FakeWechatClient
    dispatcher: FakeTaskDispatcher


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    wechat = FakeWechatClient()
    dispatcher = FakeTaskDispatcher()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[guest_auth_router.get_wechat_client] = lambda: wechat
    app.dependency_overrides[wechat_webhook_router.get_task_dispatcher] = lambda: dispatcher
    try:
        with TestClient(app) as client:
            yield Api(
                client=client,
                engine=engine,
                db_url=runtime_db_url,
                wechat=wechat,
                dispatcher=dispatcher,
            )
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


async def _fetch_order(session: AsyncSession, order_no: str) -> Order:
    return (await session.execute(select(Order).where(Order.order_no == order_no))).scalar_one()


async def _fetch_payments(session: AsyncSession, order_id: UUID) -> list[Payment]:
    stmt = (
        select(Payment)
        .where(Payment.order_id == order_id)
        .order_by(Payment.created_at.asc(), Payment.id.asc())
    )
    return list((await session.execute(stmt)).scalars())


async def _fetch_checkin_tasks(session: AsyncSession, order_id: UUID) -> list[Task]:
    stmt = select(Task).where(
        Task.task_type == "checkin.prepare", Task.dedupe_key == f"checkin.prepare:{order_id}"
    )
    return list((await session.execute(stmt)).scalars())


async def _count_identity_rows(
    session: AsyncSession, *, username: str, openid: str
) -> tuple[int, int]:
    user_count = await session.scalar(
        select(func.count()).select_from(User).where(User.username == username)
    )
    guest_count = await session.scalar(
        select(func.count()).select_from(Guest).where(Guest.openid == openid)
    )
    return int(user_count or 0), int(guest_count or 0)


async def _count_payments_by_openid(session: AsyncSession, *, openid: str) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Payment).where(Payment.openid == openid)
    )
    return int(count or 0)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _future(days: int) -> date:
    return datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)


def _derive_openid(code: str) -> str:
    return f"openid_{hashlib.sha256(code.encode()).hexdigest()[:16]}"


def _login(api: Api, *, code: str = "code-皓晨") -> dict[str, Any]:
    response = api.client.post("/api/guest/auth/wechat", json={"code": code})
    assert response.status_code == 200, response.text
    return dict(response.json())


def _seed_order(
    api: Api, *, guest_id: UUID, status: str = "created", amount_fen: int = AMOUNT_FEN
) -> str:
    """种子订单：房型 + 房间 + 订单；status 非 created 时经状态机合法迁移。"""
    order_no: str | None = None

    async def _body() -> None:
        nonlocal order_no
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            room_type = await RoomTypeRepository(session).create(
                code=f"RT-{uuid4().hex[:8].upper()}",
                name="大床房",
                base_price_fen=amount_fen // 2,
                capacity=2,
                bed_type="king",
            )
            await RoomRepository(session).create(
                room_no=f"T{uuid4().hex[:6]}",
                room_type_id=room_type.id,
                floor=3,
                lock_device_no=f"LOCK-{uuid4().hex[:8]}",
            )
            order = await OrderRepository(session).create(
                order_no=f"H{uuid4().hex[:10].upper()}",
                guest_id=guest_id,
                room_type_id=room_type.id,
                check_in_date=_future(1),
                check_out_date=_future(3),
                nights=2,
                amount_fen=amount_fen,
            )
            if status != "created":
                await OrderService(session).transition(order.id, status)
            await session.commit()
            order_no = order.order_no

    asyncio.run(_body())
    assert order_no is not None
    return order_no


def _read_order(api: Api, order_no: str) -> Order:
    return run_with_session(api.db_url, lambda session: _fetch_order(session, order_no))


def _sign(body: dict[str, Any]) -> str:
    """契约 §1.5：规范化 JSON + HMAC-SHA256（测试侧独立实现，不复用被测代码）。"""
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hmac.new(
        get_settings().device_sim_signing_secret.encode(), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _post_callback(
    api: Api,
    *,
    order_no: str,
    transaction_id: str,
    amount_fen: int,
    openid: str,
    event: str = "success",
    signature: str | None = None,
    tamper_amount: bool = False,
) -> Any:
    body: dict[str, Any] = {
        "event": event,
        "order_no": order_no,
        "transaction_id": transaction_id,
        "amount_fen": amount_fen,
        "openid": openid,
        "notified_at": datetime.now(HOTEL_TIMEZONE).isoformat(),
    }
    signature = signature if signature is not None else _sign(body)
    if tamper_amount:
        body["amount_fen"] = amount_fen + 1
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    return api.client.post(
        CALLBACK_URL,
        content=raw,
        headers={"X-Sim-Signature": signature, "Content-Type": "application/json"},
    )


def test_wechat_login_is_idempotent_and_grants_session(api: Api) -> None:
    first = _login(api, code="code-alpha")
    assert first["guest"]["openid"] == _derive_openid("code-alpha")
    assert first["user"]["role"] == "customer"
    assert first["expires_in"] == get_settings().session_ttl_seconds

    me = api.client.get("/api/auth/me", headers=_bearer(str(first["token"])))
    assert me.status_code == 200
    assert me.json()["username"] == first["user"]["username"]

    # 同一 openid 再次登录：复用既有身份与客人档案，不新建
    again = _login(api, code="code-alpha")
    assert again["user"]["id"] == first["user"]["id"]
    assert again["guest"]["id"] == first["guest"]["id"]

    counts = run_with_session(
        api.db_url,
        lambda session: _count_identity_rows(
            session, username=str(first["user"]["username"]), openid=_derive_openid("code-alpha")
        ),
    )
    assert counts == (1, 1)
    unique_guest = run_with_session(
        api.db_url,
        lambda session: GuestRepository(session).get_by_openid(_derive_openid("code-alpha")),
    )
    assert unique_guest is not None
    assert str(unique_guest.id) == first["guest"]["id"]


def test_wechat_login_maps_channel_failures(api: Api) -> None:
    api.wechat.login_error = "invalid"
    invalid = api.client.post("/api/guest/auth/wechat", json={"code": "bad-code"})
    assert invalid.status_code == 401

    api.wechat.login_error = "unavailable"
    down = api.client.post("/api/guest/auth/wechat", json={"code": "any-code"})
    assert down.status_code == 503

    # 失败不落任何身份事实
    ghost = run_with_session(
        api.db_url,
        lambda session: GuestRepository(session).get_by_openid(_derive_openid("any-code")),
    )
    assert ghost is None


def test_pay_initiation_guards_ownership_and_status(api: Api) -> None:
    login = _login(api, code="code-owner")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)

    unknown = api.client.post("/api/guest/orders/H0000000000/pay", headers=_bearer(token))
    assert unknown.status_code == 404

    # 他人订单：403（归属在服务层复验，A6）
    other = _login(api, code="code-intruder")
    intruder_token = str(other["token"])
    blocked = api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(intruder_token))
    assert blocked.status_code == 403

    anonymous = api.client.post(f"/api/guest/orders/{order_no}/pay")
    assert anonymous.status_code == 401

    # 已取消订单不可发起支付
    cancelled_no = _seed_order(api, guest_id=guest_id, status="cancelled")
    cancelled = api.client.post(f"/api/guest/orders/{cancelled_no}/pay", headers=_bearer(token))
    assert cancelled.status_code == 409

    # 正常发起：下单成功、订单进入 pending_payment、支付单落 created
    ok = api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))
    assert ok.status_code == 200, ok.text
    payload = ok.json()
    assert payload["amount_fen"] == AMOUNT_FEN
    assert payload["prepay_id"].startswith("prepay_")
    assert payload["pay_params"]["package"] == f"prepay_id={payload['prepay_id']}"

    order = _read_order(api, order_no)
    assert order.status == "pending_payment"

    def _payments(session: AsyncSession) -> list[Payment]:
        return asyncio.run(_fetch_payments(session, order.id))

    rows = run_with_session(
        api.db_url,
        lambda session: _fetch_payments(session, order.id),
    )
    assert len(rows) == 1
    assert rows[0].status == "created"
    assert rows[0].prepay_id == payload["prepay_id"]
    assert rows[0].transaction_id is None


def test_pay_initiation_channel_failure_leaves_no_facts(api: Api) -> None:
    login = _login(api, code="code-down")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)

    api.wechat.pay_error = "rejected"
    rejected = api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))
    assert rejected.status_code == 502

    api.wechat.pay_error = "unavailable"
    down = api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))
    assert down.status_code == 503

    order = _read_order(api, order_no)
    assert order.status == "created"
    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert rows == []


def test_webhook_rejects_bad_signature_without_facts(api: Api) -> None:
    login = _login(api, code="code-sign")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)
    openid = str(login["guest"]["openid"])
    api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))

    missing = _post_callback(
        api,
        order_no=order_no,
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid=openid,
        signature="",
    )
    assert missing.status_code == 401

    tampered = _post_callback(
        api,
        order_no=order_no,
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid=openid,
        tamper_amount=True,
    )
    assert tampered.status_code == 401

    order = _read_order(api, order_no)
    assert order.status == "pending_payment"
    assert order.paid_at is None
    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert [row.status for row in rows] == ["created"]
    assert api.dispatcher.enqueued == []


def test_webhook_amount_mismatch_is_rejected_without_facts(api: Api) -> None:
    login = _login(api, code="code-amount")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)
    openid = str(login["guest"]["openid"])
    api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))

    body: dict[str, Any] = {
        "event": "success",
        "order_no": order_no,
        "transaction_id": f"txn_{uuid4().hex[:12]}",
        "amount_fen": AMOUNT_FEN + 1,
        "openid": openid,
    }
    response = _post_callback(
        api,
        order_no=order_no,
        transaction_id=str(body["transaction_id"]),
        amount_fen=AMOUNT_FEN + 1,
        openid=openid,
    )
    assert response.status_code == 400

    order = _read_order(api, order_no)
    assert order.status == "pending_payment"
    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert [row.status for row in rows] == ["created"]
    assert api.dispatcher.enqueued == []


def test_webhook_applies_facts_once_and_dispatches_checkin(api: Api) -> None:
    login = _login(api, code="code-success")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)
    openid = str(login["guest"]["openid"])
    api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))

    transaction_id = f"txn_{uuid4().hex[:12]}"
    first = _post_callback(
        api,
        order_no=order_no,
        transaction_id=transaction_id,
        amount_fen=AMOUNT_FEN,
        openid=openid,
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "applied"

    # 重复投递（A3）：命中 transaction_id 幂等键，返回成功但不产生第二事实
    second = _post_callback(
        api,
        order_no=order_no,
        transaction_id=transaction_id,
        amount_fen=AMOUNT_FEN,
        openid=openid,
    )
    assert second.status_code == 200
    assert second.json()["status"] == "replayed"

    order = _read_order(api, order_no)
    assert order.status == "paid"
    assert order.paid_amount_fen == AMOUNT_FEN
    assert order.paid_at is not None
    paid_events = [event for event in order.events if event.get("type") == "paid"]
    assert len(paid_events) == 1
    assert paid_events[0]["transaction_id"] == transaction_id
    assert paid_events[0]["channel"] == "wechat"

    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert len(rows) == 1
    assert rows[0].status == "success"
    assert rows[0].transaction_id == transaction_id
    assert rows[0].notified_at is not None

    tasks = run_with_session(api.db_url, lambda session: _fetch_checkin_tasks(session, order.id))
    assert len(tasks) == 1
    assert tasks[0].status == "pending"
    assert tasks[0].handler_version == 1
    assert tasks[0].payload["order_id"] == str(order.id)
    assert tasks[0].payload["payment_id"] == str(rows[0].id)
    assert tasks[0].dedupe_key == f"checkin.prepare:{order.id}"
    assert api.dispatcher.enqueued == [tasks[0].id]


def test_out_of_order_fail_callback_does_not_regress_paid(api: Api) -> None:
    login = _login(api, code="code-outoforder")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)
    openid = str(login["guest"]["openid"])
    api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))

    success = _post_callback(
        api,
        order_no=order_no,
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid=openid,
    )
    assert success.status_code == 200
    assert success.json()["status"] == "applied"

    late_fail = _post_callback(
        api,
        order_no=order_no,
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid=openid,
        event="fail",
    )
    assert late_fail.status_code == 200
    assert late_fail.json()["status"] == "already_paid"

    order = _read_order(api, order_no)
    assert order.status == "paid"
    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert [row.status for row in rows] == ["success"]
    assert len(run_with_session(api.db_url, lambda s: _fetch_checkin_tasks(s, order.id))) == 1
    assert len(api.dispatcher.enqueued) == 1


def test_failed_payment_keeps_order_payable(api: Api) -> None:
    login = _login(api, code="code-fail")
    token = str(login["token"])
    guest_id = UUID(str(login["guest"]["id"]))
    order_no = _seed_order(api, guest_id=guest_id)
    openid = str(login["guest"]["openid"])
    api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))

    failed = _post_callback(
        api,
        order_no=order_no,
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid=openid,
        event="fail",
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"

    order = _read_order(api, order_no)
    assert order.status == "pending_payment"
    assert order.paid_at is None
    rows = run_with_session(api.db_url, lambda session: _fetch_payments(session, order.id))
    assert [row.status for row in rows] == ["failed"]
    assert run_with_session(api.db_url, lambda s: _fetch_checkin_tasks(s, order.id)) == []

    # 失败后可再次发起支付
    retry = api.client.post(f"/api/guest/orders/{order_no}/pay", headers=_bearer(token))
    assert retry.status_code == 200


def test_unknown_order_callback_is_acknowledged_without_facts(api: Api) -> None:
    response = _post_callback(
        api,
        order_no="H0000000000",
        transaction_id=f"txn_{uuid4().hex[:12]}",
        amount_fen=AMOUNT_FEN,
        openid="openid_ghost",
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unknown_order"
    assert api.dispatcher.enqueued == []
    ghost_payments = run_with_session(
        api.db_url, lambda s: _count_payments_by_openid(s, openid="openid_ghost")
    )
    assert ghost_payments == 0
