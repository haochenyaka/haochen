"""入住通知集成测试（真实 PostgreSQL + 门锁/微信替身 + 真实任务执行器）。

覆盖：支付确认后经订阅消息下发房号楼层、开门密码与入住指引（指引优先取自知识库、
检索不可用时退回固定文案）；渠道失败不重试、不改任务结果，主通道（凭证签发）保持有效，
触达结果以订单事件 `credential_notified` 留痕；未配置模板或缺少微信身份时跳过并留痕；
下发载荷明文既不落任务结果也不落库（A2）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.knowledge.retrieval import RetrievedChunk, RetrievedContext
from app.lock import task_handlers as lock_task_handlers
from app.models.domain import Guest, Order, Room
from app.repositories import (
    GuestRepository,
    KnowledgeBaseRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.services import checkin_notice_service
from app.services.checkin_notice_service import GUIDE_FALLBACK, NOTIFIED_EVENT
from app.services.order_service import OrderService
from app.services.task_service import TaskExecutor, submit_task
from app.wechat import SubscribeMessage, WechatError, WechatUnavailableError
from tests.integration.db_utils import run_with_session_factory
from tests.integration.test_lock_credentials import (
    FakeLockClient,
    _ensure_checkin_handler_registered,
    _StaticLockClient,
)

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class FakeWechatClient:
    """微信订阅消息替身：逐条记录下发载荷，可注入渠道失败。"""

    def __init__(self) -> None:
        self.error: WechatError | None = None
        self.sent: list[dict[str, Any]] = []

    @classmethod
    def from_settings(cls) -> FakeWechatClient:
        return cls()

    async def aclose(self) -> None:
        return None

    async def send_subscribe_message(
        self,
        *,
        openid: str,
        template_id: str,
        data: dict[str, str],
        page: str | None = None,
    ) -> SubscribeMessage:
        if self.error is not None:
            raise self.error
        self.sent.append(
            {
                "openid": openid,
                "template_id": template_id,
                "page": page,
                "data": dict(data),
            }
        )
        return SubscribeMessage(message_id=f"msg-{len(self.sent)}", status="sent")


class _StaticWechatClient:
    """把固定替身注入任务处理器的模块级 `WechatClient.from_settings()` 调用点。"""

    def __init__(self, client: FakeWechatClient) -> None:
        self._client = client

    def from_settings(self) -> FakeWechatClient:
        return self._client


def _future(days: int) -> date:
    return datetime.now(HOTEL_TIMEZONE).date() + timedelta(days=days)


async def _guest(session: AsyncSession, *, openid: str | None) -> Guest:
    user = await UserRepository(session).get_or_create(username=f"notice-{uuid4().hex[:8]}")
    guest = await GuestRepository(session).get_or_create_by_user(user.id)
    if openid is not None:
        await GuestRepository(session).update_fields(guest.id, {"openid": openid})
        guest = await GuestRepository(session).get_by_id(guest.id)
        assert guest is not None
    return guest


async def _seed_paid_order(session: AsyncSession, *, guest_id: UUID) -> tuple[Order, Room]:
    room_type = await RoomTypeRepository(session).create(
        code=f"RT-{uuid4().hex[:8].upper()}",
        name="大床房",
        base_price_fen=39800,
        capacity=2,
        bed_type="king",
    )
    room = await RoomRepository(session).create(
        room_no=f"N{uuid4().hex[:6].upper()}",
        room_type_id=room_type.id,
        floor=6,
        lock_device_no=f"LOCK-{uuid4().hex[:8]}",
    )
    order = await OrderRepository(session).create(
        order_no=f"H{uuid4().hex[:10].upper()}",
        guest_id=guest_id,
        room_type_id=room_type.id,
        check_in_date=_future(1),
        check_out_date=_future(3),
        nights=2,
        amount_fen=79600,
    )
    service = OrderService(session)
    await service.transition(order.id, "pending_payment")
    order = await service.transition(order.id, "paid")
    return order, room


async def _run_checkin(factory: SessionFactoryArg, order_id: UUID) -> Any:
    submission = await submit_task(
        task_type=lock_task_handlers.CHECKIN_TASK_TYPE,
        handler_version=lock_task_handlers.CHECKIN_HANDLER_VERSION,
        payload={"order_id": str(order_id), "payment_id": str(uuid4())},
        dedupe_key=f"checkin.prepare:{order_id}",
        session_factory=factory,
    )
    return await TaskExecutor(session_factory=factory).execute(submission.task_id)


async def _events(factory: SessionFactoryArg, order_id: UUID, event_type: str) -> list[Any]:
    async with factory() as session:
        order = await OrderRepository(session).get_by_id(order_id)
        assert order is not None
        return [event for event in order.events if event.get("type") == event_type]


async def _raw_rows(factory: SessionFactoryArg, *, room_no: str) -> list[str]:
    async with factory() as session:
        result = await session.execute(
            text("SELECT * FROM lock_credentials WHERE room_no = :room_no"), {"room_no": room_no}
        )
        return [str(tuple(row)) for row in result]


def _configure(monkeypatch: pytest.MonkeyPatch, *, template_id: str) -> None:
    monkeypatch.setattr(get_settings(), "wechat_subscribe_template_id", template_id)


def test_notify_sends_password_and_guide_on_paid_order(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_checkin_handler_registered()
    lock = FakeLockClient()
    wechat = FakeWechatClient()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(lock))
    monkeypatch.setattr(lock_task_handlers, "WechatClient", _StaticWechatClient(wechat))
    _configure(monkeypatch, template_id="tmpl-checkin-v1")

    async def _run(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session, openid="openid-notice-1")
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, room_no = order.id, room.room_no

        outcome = await _run_checkin(factory, order_id)
        async with factory() as session:
            reloaded = await OrderRepository(session).get_by_id(order_id)
            assert reloaded is not None
            events_text = str(reloaded.events)
        return {
            "outcome": outcome,
            "events": await _events(factory, order_id, NOTIFIED_EVENT),
            "sent": list(wechat.sent),
            "room_no": room_no,
            "device_code": lock.codes[room_no],
            "raw": await _raw_rows(factory, room_no=room_no),
            "order_events_text": events_text,
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    assert len(values["sent"]) == 1
    payload = values["sent"][0]
    assert payload["openid"] == "openid-notice-1"
    assert payload["template_id"] == "tmpl-checkin-v1"
    assert payload["page"] == "pages/orders/index"
    assert payload["data"]["room_no"] == values["room_no"]
    assert payload["data"]["floor"] == "6"
    assert payload["data"]["code"] == values["device_code"]
    assert payload["data"]["guide"] == GUIDE_FALLBACK  # 未种子知识库时退回固定文案

    (event,) = values["events"]
    assert event == {
        "type": NOTIFIED_EVENT,
        "channel": "wechat_subscribe",
        "status": "sent",
        "template_id": "tmpl-checkin-v1",
    }
    # A2：下发载荷明文不落库（凭证行与订单事件都扫不到）
    assert values["raw"] and all(values["device_code"] not in row for row in values["raw"])
    assert values["device_code"] not in values["order_events_text"]


def test_notify_guide_uses_knowledge_base_snippets(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_checkin_handler_registered()
    lock = FakeLockClient()
    wechat = FakeWechatClient()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(lock))
    monkeypatch.setattr(lock_task_handlers, "WechatClient", _StaticWechatClient(wechat))
    _configure(monkeypatch, template_id="tmpl-checkin-v1")

    class _FakeRetriever:
        """两段式检索替身：返回两段带换行的知识库原文，供指引拼接口径断言。"""

        def __init__(self, session: Any, **kwargs: Any) -> None:
            self._session = session

        async def retrieve(self, query: str, **kwargs: Any) -> RetrievedContext:
            return RetrievedContext(
                query=query,
                chunks=tuple(
                    RetrievedChunk(
                        index=index,
                        chunk_id=uuid4(),
                        kb_id=uuid4(),
                        document_id=uuid4(),
                        document_title="网络与智能设备",
                        ordinal=index,
                        content=content,
                        score=1.0,
                        recall_score=1.0,
                        char_start=None,
                        char_end=None,
                    )
                    for index, content in enumerate(
                        (
                            "## Wi-Fi\n客房 Wi-Fi 名称：HaoChen-Guest，密码见房卡。",
                            "退房时间 12:00 前。",
                        ),
                        start=1,
                    )
                ),
                prompt_text="",
            )

    monkeypatch.setattr(checkin_notice_service, "KnowledgeRetriever", _FakeRetriever)

    async def _run(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            await KnowledgeBaseRepository(session).create(
                code="hotel-guest", name="宾客知识库", scope="guest"
            )
            guest = await _guest(session, openid="openid-notice-2")
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id = order.id

        outcome = await _run_checkin(factory, order_id)
        return {"outcome": outcome, "sent": list(wechat.sent)}

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    guide = values["sent"][0]["data"]["guide"]
    assert "HaoChen-Guest" in guide
    assert "退房时间 12:00 前" in guide
    assert "\n" not in guide  # 片段按空白折叠后以分号拼接
    assert "#" not in guide  # 知识库原文的 Markdown 标记不进入通知字段
    assert guide != GUIDE_FALLBACK


def test_notify_failure_keeps_task_and_main_channel_intact(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """契约 §8-11：订阅消息失败不重试、不影响任务结果与凭证签发，失败以事件留痕。"""
    _ensure_checkin_handler_registered()
    lock = FakeLockClient()
    wechat = FakeWechatClient()
    wechat.error = WechatUnavailableError("微信渠道不可达：/wechat/v1/notify/subscribe")
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(lock))
    monkeypatch.setattr(lock_task_handlers, "WechatClient", _StaticWechatClient(wechat))
    _configure(monkeypatch, template_id="tmpl-checkin-v1")

    async def _run(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            guest = await _guest(session, openid="openid-notice-3")
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_id, room_no = order.id, room.room_no

        outcome = await _run_checkin(factory, order_id)
        events = await _events(factory, order_id, NOTIFIED_EVENT)
        async with factory() as session:
            reloaded = await OrderRepository(session).get_by_id(order_id)
            assert reloaded is not None
            status, room_id = reloaded.status, reloaded.room_id
        return {
            "outcome": outcome,
            "events": events,
            "sent": list(wechat.sent),
            "status": status,
            "room_id": room_id,
            "room_no": room_no,
            "device_code": lock.codes[room_no],
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["outcome"] == "succeeded"
    assert values["sent"] == []
    (event,) = values["events"]
    assert event["status"] == "failed"
    assert "微信渠道不可达" in str(event["reason"])
    assert event["template_id"] == "tmpl-checkin-v1"
    # 主通道不受影响：凭证已签发且订单仍持有分配；事件与结果都不含明文
    assert values["status"] == "paid"
    assert values["room_id"] is not None
    assert values["device_code"] not in str(values["events"])


def test_notify_skips_without_template_or_guest_openid(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_checkin_handler_registered()
    lock = FakeLockClient()
    wechat = FakeWechatClient()
    monkeypatch.setattr(lock_task_handlers, "LockClient", _StaticLockClient(lock))
    monkeypatch.setattr(lock_task_handlers, "WechatClient", _StaticWechatClient(wechat))

    async def _run(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            with_openid = await _guest(session, openid="openid-notice-4")
            without_openid = await _guest(session, openid=None)
            first, _ = await _seed_paid_order(session, guest_id=with_openid.id)
            second, _ = await _seed_paid_order(session, guest_id=without_openid.id)
            await session.commit()
            first_id, second_id = first.id, second.id

        _configure(monkeypatch, template_id="")
        await _run_checkin(factory, first_id)
        _configure(monkeypatch, template_id="tmpl-checkin-v1")
        await _run_checkin(factory, second_id)
        return {
            "no_template": await _events(factory, first_id, NOTIFIED_EVENT),
            "no_openid": await _events(factory, second_id, NOTIFIED_EVENT),
            "sent": list(wechat.sent),
        }

    values = run_with_session_factory(runtime_db_url, _run)
    assert values["sent"] == []  # 跳过不下发
    (first,) = values["no_template"]
    assert first["status"] == "skipped"
    assert first["reason"] == "未配置订阅消息模板"
    assert "template_id" not in first
    (second,) = values["no_openid"]
    assert second["status"] == "skipped"
    assert "微信身份" in str(second["reason"])
    assert second["template_id"] == "tmpl-checkin-v1"
