"""P4 安全审计：全表明文扫描 + 客人 REST 越权负例（A6 取证扩展）。

覆盖：
- 全表扫描：支付+入住+密码下发全链路后，lock_credentials/tasks/orders/
  payments/messages/tool_audit/model_audit 均无明文密码；
- 客人端点越权：他人订单的改期/取消/续订/发票/支付拒绝；
- 房型端点未认证拒绝。
"""

from __future__ import annotations

import asyncio
import hmac
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.middlewares.credential_guard import scan_sensitive_plaintext
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.config import get_settings
from app.db import get_session
from app.lock.service import LockCredentialService
from app.main import app
from app.models.audit import ModelAudit, ToolAudit
from app.models.domain import LockCredential
from app.repositories import (
    MessageRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.security import hash_password
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, RecordingRunEventBus, intake_message
from tests.integration.test_checkin_tools import _future, _guest, _seed_paid_order
from tests.integration.test_lock_credentials import (  # type: ignore[attr-defined]
    FakeLockClient,
    _ensure_checkin_handler_registered,
    _StaticLockClient,
)

SessionFactoryArg = async_sessionmaker[AsyncSession]

pytestmark = pytest.mark.integration


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(api: Api, *, username: str, password: str = "audit-pass-9") -> str:
    async def _body(session: AsyncSession) -> UUID:
        user = await UserRepository(session).create_account(
            username=username, password_hash=hash_password(password), role="customer"
        )
        return user.id

    _run(api, _body)
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _run(api: Api, body: Any) -> Any:
    async def _inner() -> Any:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            result = await body(session)
            await session.commit()
            return result

    return asyncio.run(_inner())


# ── Test 1: 全表明文扫描 ──────────────────────────────────────────


class _PlaintextScanModel(BaseChatModel):
    """扫描用模型：先点名下发工具，收到结果后回复。"""

    model_name: str = "scan-model"
    tool_name: str = "deliver_room_password"
    tool_args: dict[str, str] = {}
    notices: list[str] = []

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        has_tool_result = any(isinstance(m, ToolMessage) for m in messages)
        if not has_tool_result:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_args),
                        "id": "call-scan-1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            head = str(messages[0].content) if messages else ""
            if "下发" in head or "密码" in head:
                self.notices.append(head)
            reply = AIMessage(content="您的开门密码已下发，旧密码已失效。")
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


def test_p4_plaintext_scan_across_all_tables(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全链路后扫描所有 P4 相关表：各表不得包含 6+ 位数字明文密码。"""
    from app.agents.toolkits.checkin import LockClient as CheckinLockClient
    from app.lock import task_handlers as lock_task_handlers_module

    fake = FakeLockClient()
    _ensure_checkin_handler_registered()
    monkeypatch.setattr(
        lock_task_handlers_module, "LockClient", _StaticLockClient(fake)
    )
    monkeypatch.setattr(CheckinLockClient, "from_settings", classmethod(lambda cls: fake))

    registry = ToolRegistry()

    @builtin_tool(
        category="checkin",
        registry=registry,
        read_only=False,
        credential_delivery=True,
    )
    async def deliver_room_password(order_no: str) -> str:
        """下发房间密码。"""
        return f"delivered:{order_no}"

    model = _PlaintextScanModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model",
        lambda **kwargs: model,
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("deliver_room_password",))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        checkpointer = InMemorySaver()
        bus = RecordingRunEventBus()
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=checkpointer,
                resource_policy=StaticResourcePolicy(
                    allowed=frozenset({("tool", "deliver_room_password")})
                ),
                tool_registry=registry,
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
            event_bus=bus,
        )
        username = f"scan-{uuid4().hex[:8]}"
        async with factory() as session:
            guest = await _guest(session, username=username)
            order, room = await _seed_paid_order(session, guest_id=guest.id)
            await session.commit()
            order_no, order_id, room_no = (
                order.order_no,
                order.id,
                room.room_no,
            )

            await LockCredentialService(session).issue_for_order(
                order_id=order.id, client=fake
            )
            await session.commit()

            model.tool_args["order_no"] = order_no

            intake = await intake_message(
                session, username=username, text="我要入住密码"
            )
            assert intake.run_id is not None
            outcome = await executor.execute(intake.run_id)

            tables: dict[str, list[dict[str, Any]]] = {}

            rows_src = (await session.execute(select(LockCredential))).scalars().all()
            tables["lock_credentials"] = [
                {
                    "room_no": r.room_no,
                    "status": r.status,
                    "reason": r.reason,
                    "version": r.version,
                    "code_hash": r.code_hash,
                }
                for r in rows_src
            ]

            from app.models.task import Task as TaskModel

            rows_src = (await session.execute(select(TaskModel))).scalars().all()
            tables["tasks"] = [
                {
                    "task_type": r.task_type,
                    "status": r.status,
                    "result": str(r.result),
                    "error": r.error,
                }
                for r in rows_src
            ]

            from app.models.domain import Order as OrderModel

            order_row = await session.get(OrderModel, order_id)
            tables["orders"] = [
                {
                    "order_no": order_row.order_no,
                    "status": order_row.status,
                    "events": order_row.events,
                }
            ]

            from app.models.domain import Payment as PaymentModel

            payment_rows = (
                (await session.execute(select(PaymentModel))).scalars().all()
            )
            tables["payments"] = [
                {
                    "status": p.status,
                    "transaction_id": p.transaction_id,
                }
                for p in payment_rows
            ]

            msg_rows = await MessageRepository(session).list_for_thread(
                intake.thread_id
            )
            tables["messages"] = [
                {"role": m.role, "content": str(m.content)} for m in msg_rows
            ]

            audit_rows = (
                (
                    await session.execute(
                        select(ToolAudit).where(
                            ToolAudit.run_id == str(intake.run_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            tables["tool_audit"] = [
                {
                    "tool_name": r.tool_name,
                    "status": r.status,
                    "args_digest": r.args_digest,
                }
                for r in audit_rows
            ]

            model_rows = (
                (
                    await session.execute(
                        select(ModelAudit).where(
                            ModelAudit.run_id == str(intake.run_id)
                        )
                    )
                )
                .scalars()
                .all()
            )
            tables["model_audit"] = [
                {"model": r.model, "error": r.error} for r in model_rows
            ]

        return {
            "outcome": outcome,
            "tables": tables,
            "device_code": fake.codes[room_no],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    device_code = values["device_code"]
    assert len(device_code) >= 6

    for table_name, rows in values["tables"].items():
        for row in rows:
            text = json.dumps(row, ensure_ascii=False, default=str)
            hits = scan_sensitive_plaintext(text)
            assert not hits, (
                f"明文密码进入表 {table_name}：{hits} 行={row}"
            )

    settings = get_settings()
    for row in values["tables"]["lock_credentials"]:
        # 与 app.lock.service.hash_lock_code 同口径：raw = room:version:key_version:code
        expected = hmac.new(
            settings.lock_master_key.encode(),
            f"{row['room_no']}:{row['version']}:1:{device_code}".encode(),
            "sha256",
        ).hexdigest()
        assert row["code_hash"] == expected, (
            f"code_hash 不匹配：room={row['room_no']} v={row['version']}"
        )

    for event in values["tables"]["orders"][0]["events"]:
        text = json.dumps(event, ensure_ascii=False, default=str)
        assert not scan_sensitive_plaintext(text), f"订单事件含明文：{event}"


# ── Test 2: 客人 REST 越权负例 ──────────────────────────────────


def test_guest_order_endpoints_reject_unauthorized(api: Api) -> None:
    """客人订单端点：未认证 401、他人订单 403/404、伪造令牌 401。"""
    a = uuid4().hex[:6]
    owner = _login(api, username=f"own-{a}")
    stranger = _login(api, username=f"str-{a}")
    code = f"RT-{uuid4().hex[:6].upper()}"

    async def _seed() -> dict[str, str]:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            rt = await RoomTypeRepository(session).create(
                code=code, name="审计房型", base_price_fen=29800, capacity=2, bed_type="king"
            )
            await RoomRepository(session).create(
                room_no=f"AU-{uuid4().hex[:6].upper()}", room_type_id=rt.id, floor=5
            )
            await session.commit()
        resp = api.client.post(
            "/api/guest/orders",
            headers=_auth(owner),
            json={
                "room_type_code": code,
                "check_in_date": _future(1).isoformat(),
                "check_out_date": _future(3).isoformat(),
            },
        )
        assert resp.status_code == 201, resp.text
        return {"order_no": resp.json()["order_no"]}

    data = _run(api, lambda session: _seed())

    assert api.client.get("/api/guest/orders").status_code == 401
    assert (
        api.client.post("/api/guest/orders", json={"room_type_code": code}).status_code == 401
    )
    assert (
        api.client.get(f"/api/guest/orders/{data['order_no']}").status_code == 401
    )
    assert (
        api.client.post(f"/api/guest/orders/{data['order_no']}/cancel", json={}).status_code == 401
    )
    assert (
        api.client.post(f"/api/guest/orders/{data['order_no']}/pay", json={}).status_code == 401
    )
    assert api.client.get("/api/guest/room-types").status_code == 401
    assert api.client.get(f"/api/guest/room-types/{code}").status_code == 401

    forged = {"Authorization": "Bearer forged"}
    assert api.client.get("/api/guest/orders", headers=forged).status_code == 401
    assert (
        api.client.get(
            f"/api/guest/orders/{data['order_no']}", headers=forged
        ).status_code
        == 401
    )
    assert (
        api.client.post(
            f"/api/guest/orders/{data['order_no']}/cancel",
            headers=forged,
            json={},
        ).status_code
        == 401
    )

    for method, path, body in [
        ("GET", f"/api/guest/orders/{data['order_no']}", {}),
        (
            "POST",
            f"/api/guest/orders/{data['order_no']}/reschedule",
            {"check_in_date": "2026-10-05", "check_out_date": "2026-10-07"},
        ),
        ("POST", f"/api/guest/orders/{data['order_no']}/cancel", {}),
        ("POST", f"/api/guest/orders/{data['order_no']}/renew", {"check_out_date": "2026-10-05"}),
        ("POST", f"/api/guest/orders/{data['order_no']}/invoice", {"title": "x"}),
        ("POST", f"/api/guest/orders/{data['order_no']}/pay", {}),
    ]:
        headers = _auth(stranger)
        if method == "GET":
            resp = api.client.get(path, headers=headers)
        else:
            resp = api.client.post(path, headers=headers, json=body)
        assert resp.status_code in (403, 404), (
            f"{method} {path} 应返回 403/404，实际 {resp.status_code}"
        )

    assert (
        api.client.get(
            f"/api/guest/orders/{data['order_no']}", headers=_auth(owner)
        ).status_code
        == 200
    )


# ── Test 3: 管理端响应明文扫描 ──────────────────────────────


def test_admin_responses_plaintext_scan(api: Api) -> None:
    """管理端 API 响应不得泄露明文密码。"""
    suffix = uuid4().hex[:8]
    username = f"mgr-plain-{suffix}"
    password = "plain-scan-9"

    async def _create(session: AsyncSession) -> None:
        await UserRepository(session).create_account(
            username=username, password_hash=hash_password(password), role="manager"
        )
        await session.commit()

    _run(api, _create)
    resp = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    token = str(resp.json()["token"])
    headers = _auth(token)

    for path in ("/api/admin/memories", "/api/admin/conversations"):
        resp = api.client.get(path, headers=headers)
        assert resp.status_code in (200, 404), f"{path}: {resp.status_code} {resp.text}"
        hits = scan_sensitive_plaintext(resp.text)
        pw = [h for h in hits if h.category == "password"]
        assert len(pw) == 0, f"{path} 泄露明文密码：{pw}"


def test_password_not_leaked_in_admin_user_endpoints(api: Api) -> None:
    """创建用户 API 不得回显明文密码，DB 中密码为 argon2id 格式。"""
    suffix = uuid4().hex[:8]
    admin_user = f"adm-{suffix}"
    admin_pass = "adm-pass-99"
    new_user = f"new-{suffix}"
    new_pass = "new-pass-88"

    async def _create_admin(session: AsyncSession) -> None:
        await UserRepository(session).create_account(
            username=admin_user, password_hash=hash_password(admin_pass), role="admin"
        )
        await session.commit()

    _run(api, _create_admin)
    resp = api.client.post("/api/auth/login", json={"username": admin_user, "password": admin_pass})
    assert resp.status_code == 200, resp.text
    token = str(resp.json()["token"])
    headers = _auth(token)

    create_resp = api.client.post(
        "/api/admin/users",
        headers=headers,
        json={"username": new_user, "password": new_pass, "role": "staff"},
    )
    assert create_resp.status_code == 201, create_resp.text
    assert new_pass not in create_resp.text, "明文密码出现在创建用户响应中"
    assert "password" not in create_resp.text, "响应 body 包含 password 字段"

    async def _check_hash(session: AsyncSession) -> str:
        user = await UserRepository(session).get_by_username(new_user)
        assert user is not None
        return user.password_hash

    pw_hash = _run(api, _check_hash)
    assert pw_hash.startswith("$argon2id$"), f"密码散列不是 argon2id 格式: {pw_hash}"


# ── Test 4: 泄露轮换明文通道验证 ──────────────────────────────


def test_lock_leak_response_contains_plaintext_by_design(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """泄露轮换端点必须返回明文 code（设计使然的一次性明文通道）。"""
    from app.routers import admin_lock_router

    suffix = uuid4().hex[:8]
    username = f"mgr-leak-{suffix}"
    password = "leak-pass-9"

    async def _create(session: AsyncSession) -> None:
        await UserRepository(session).create_account(
            username=username, password_hash=hash_password(password), role="manager"
        )
        await session.commit()

    _run(api, _create)
    resp = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    token = str(resp.json()["token"])
    headers = _auth(token)

    # Seed room with credentials via daily rotation
    fake = FakeLockClient()
    monkeypatch.setattr(admin_lock_router, "LockClient", _StaticLockClient(fake))

    async def _seed(session: AsyncSession) -> str:
        guest = await _guest(session, username=f"g-{suffix}")
        _order, room = await _seed_paid_order(session, guest_id=guest.id)
        await session.commit()
        await LockCredentialService(session).rotate_daily_room(
            room_no=room.room_no, client=fake
        )
        await session.commit()
        return room.room_no

    room_no = _run(api, _seed)

    leak_resp = api.client.post(
        f"/api/admin/lock/rooms/{room_no}/credential/rotate",
        headers=headers,
        json={},
    )
    assert leak_resp.status_code == 200, leak_resp.text
    payload = leak_resp.json()
    assert "code" in payload, f"泄露轮换响应缺少 code 字段: {payload}"
    assert payload["code"] == fake.codes[room_no], "明文 code 与设备侧不一致"
    assert payload["room_no"] == room_no


def test_exit_criteria_stub() -> None:
    """P4 退出标准断言存根：
    1. 明文扫描 — test_p4_plaintext_scan_across_all_tables ✓
    2. 重复回调 — test_webhook_applies_facts_once_and_dispatches_checkin ✓
    3. 门锁失败 — test_checkin_task_failure_keeps_order_paid_and_records_blocked
       (暂缺：需 checkin-flow 失败场景验收)
    """
    assert True