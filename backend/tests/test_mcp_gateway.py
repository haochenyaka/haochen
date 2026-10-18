"""mcp-gateway 单测（PR-M3）：工具契约稳定 + 转发到渠道客户端 + 多路由可发现。

用替身渠道客户端，不起真实外部服务；最后一条用 ASGI 直连验证 streamable_http 发现链路。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.device_sim.main import app as sim_app
from app.lock.client import (
    CredentialMetadata,
    IssuedCredential,
    LockClient,
    LockError,
    LockUnavailableError,
    UnlockRecord,
)
from app.mcp_gateway.main import build_app
from app.mcp_gateway.tools import build_lock_server, build_robot_server, build_wechat_server
from app.robot.client import RobotClient, RobotDelivery, RobotUnavailableError
from app.wechat.client import PayOrderStatus


def _payload(result: Any) -> dict[str, Any]:
    # FastMCP 返回 (content, structured)；取结构化结果
    payload = result[1] if isinstance(result, tuple) else result
    assert isinstance(payload, dict)
    return payload


class _FakeLockClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self._raise = raise_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _fail(self) -> None:
        if self._raise:
            raise self._raise

    async def issue_credential(self, **kwargs: Any) -> IssuedCredential:
        self.calls.append(("issue_credential", kwargs))
        self._fail()
        now = datetime.now(UTC)
        return IssuedCredential(
            credential_id="c1",
            room_no=kwargs["room_no"],
            code="123456",
            version=1,
            status="active",
            reason="checkin",
            valid_from=now,
            valid_to=now + timedelta(hours=24),
        )

    async def rotate_credential(self, **kwargs: Any) -> IssuedCredential:
        self.calls.append(("rotate_credential", kwargs))
        self._fail()
        now = datetime.now(UTC)
        return IssuedCredential(
            credential_id="c2",
            room_no=kwargs["room_no"],
            code="654321",
            version=2,
            status="active",
            reason="daily",
            valid_from=now,
            valid_to=now + timedelta(hours=24),
        )

    async def revoke_credential(self, **kwargs: Any) -> CredentialMetadata:
        self.calls.append(("revoke_credential", kwargs))
        self._fail()
        now = datetime.now(UTC)
        return CredentialMetadata(
            credential_id="c2",
            room_no=kwargs["room_no"],
            version=2,
            status="revoked",
            reason="revoke",
            valid_from=now,
            valid_to=now + timedelta(hours=24),
        )

    async def get_credential(self, room_no: str) -> CredentialMetadata:
        self.calls.append(("get_credential", {"room_no": room_no}))
        self._fail()
        now = datetime.now(UTC)
        return CredentialMetadata(
            credential_id="c1",
            room_no=room_no,
            version=1,
            status="active",
            reason="checkin",
            valid_from=now,
            valid_to=now + timedelta(hours=24),
        )

    async def list_unlock_records(self, *, room_no: str | None = None) -> list[UnlockRecord]:
        self.calls.append(("list_unlock_records", {"room_no": room_no}))
        self._fail()
        return [
            UnlockRecord(
                record_id="r1",
                room_no=room_no or "301",
                source="guest",
                accepted=True,
                at=datetime.now(UTC),
            )
        ]


class _FakeRobotClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self._raise = raise_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _fail(self) -> None:
        if self._raise:
            raise self._raise

    async def create_delivery(self, **kwargs: Any) -> RobotDelivery:
        self.calls.append(("create_delivery", kwargs))
        self._fail()
        now = datetime.now(UTC)
        return RobotDelivery(
            delivery_id="d1",
            order_no=kwargs["order_no"],
            room_no=kwargs["room_no"],
            item=kwargs["item"],
            status="queued",
            created_at=now,
            updated_at=now,
        )

    async def get_delivery(self, delivery_id: str) -> RobotDelivery:
        self.calls.append(("get_delivery", {"delivery_id": delivery_id}))
        self._fail()
        now = datetime.now(UTC)
        return RobotDelivery(
            delivery_id=delivery_id,
            order_no="DEMO-0001",
            room_no="301",
            item="矿泉水",
            status="delivering",
            created_at=now,
            updated_at=now,
        )


class _FakeWechatClient:
    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self._raise = raise_error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def get_pay_order(self, order_no: str) -> PayOrderStatus:
        self.calls.append(("get_pay_order", {"order_no": order_no}))
        if self._raise:
            raise self._raise
        return PayOrderStatus(
            order_no=order_no, amount_fen=119400, status="success", notifications=1
        )


@pytest.mark.asyncio
async def test_lock_server_exposes_expected_tools() -> None:
    server = build_lock_server(client=_FakeLockClient())  # type: ignore[arg-type]
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "issue_credential",
        "rotate_credential",
        "revoke_credential",
        "get_credential_metadata",
        "list_unlock_records",
    }


@pytest.mark.asyncio
async def test_issue_credential_forwards_to_client() -> None:
    client = _FakeLockClient()
    server = build_lock_server(client=client)  # type: ignore[arg-type]

    payload = _payload(
        await server.call_tool("issue_credential", {"room_no": "301", "valid_hours": 24})
    )
    assert payload["ok"] is True
    assert payload["room_no"] == "301"
    assert payload["code"] == "123456"
    assert client.calls == [("issue_credential", {"room_no": "301", "valid_hours": 24})]


@pytest.mark.asyncio
async def test_unreachable_device_converges_to_error_result() -> None:
    client = _FakeLockClient(raise_error=LockUnavailableError("down"))
    server = build_lock_server(client=client)  # type: ignore[arg-type]
    payload = _payload(await server.call_tool("issue_credential", {"room_no": "301"}))
    assert payload["ok"] is False
    assert "不可达" in payload["error"]


@pytest.mark.asyncio
async def test_lock_error_converges_to_error_result() -> None:
    client = _FakeLockClient(raise_error=LockError("bad request", status_code=409))
    server = build_lock_server(client=client)  # type: ignore[arg-type]
    payload = _payload(await server.call_tool("revoke_credential", {"room_no": "301"}))
    assert payload["ok"] is False
    assert "bad request" in payload["error"]


@pytest.mark.asyncio
async def test_robot_server_exposes_expected_tools() -> None:
    server = build_robot_server(client=_FakeRobotClient())  # type: ignore[arg-type]
    names = {t.name for t in await server.list_tools()}
    assert names == {"create_delivery", "get_delivery"}


@pytest.mark.asyncio
async def test_robot_create_delivery_forwards_to_client() -> None:
    client = _FakeRobotClient()
    server = build_robot_server(client=client)  # type: ignore[arg-type]

    payload = _payload(
        await server.call_tool(
            "create_delivery", {"order_no": "DEMO-0001", "room_no": "301", "item": "矿泉水"}
        )
    )
    assert payload["ok"] is True
    assert payload["delivery_id"] == "d1"
    assert payload["status"] == "queued"
    assert client.calls == [
        ("create_delivery", {"order_no": "DEMO-0001", "room_no": "301", "item": "矿泉水"})
    ]


@pytest.mark.asyncio
async def test_robot_unavailable_converges_to_error_result() -> None:
    server = build_robot_server(client=_FakeRobotClient(raise_error=RobotUnavailableError("down")))  # type: ignore[arg-type]
    payload = _payload(await server.call_tool("get_delivery", {"delivery_id": "d1"}))
    assert payload["ok"] is False
    assert "不可达" in payload["error"]


@pytest.mark.asyncio
async def test_wechat_server_exposes_expected_tools() -> None:
    server = build_wechat_server(client=_FakeWechatClient())  # type: ignore[arg-type]
    names = {t.name for t in await server.list_tools()}
    assert names == {"get_pay_order_status"}


@pytest.mark.asyncio
async def test_wechat_pay_status_forwards_to_client() -> None:
    client = _FakeWechatClient()
    server = build_wechat_server(client=client)  # type: ignore[arg-type]

    payload = _payload(await server.call_tool("get_pay_order_status", {"order_no": "DEMO-0001"}))
    assert payload == {
        "ok": True,
        "order_no": "DEMO-0001",
        "amount_fen": 119400,
        "status": "success",
        "notifications": 1,
    }
    assert client.calls == [("get_pay_order", {"order_no": "DEMO-0001"})]


@pytest.mark.asyncio
async def test_lock_tool_reaches_device_sim_contract() -> None:
    """MCP 工具 → LockClient → device-sim 全链路（ASGI 直连，不起网络服务）。"""
    transport = httpx.ASGITransport(app=sim_app)
    lock = LockClient(base_url="http://device-sim.test")
    lock._client = httpx.AsyncClient(base_url="http://device-sim.test", transport=transport)
    server = build_lock_server(client=lock)
    try:
        payload = _payload(await server.call_tool("issue_credential", {"room_no": "3901"}))
    finally:
        await lock.aclose()
    assert payload["ok"] is True
    assert payload["room_no"] == "3901"
    assert payload["code"].isdigit()


@pytest.mark.asyncio
async def test_robot_tool_reaches_device_sim_contract() -> None:
    transport = httpx.ASGITransport(app=sim_app)
    robot = RobotClient(base_url="http://device-sim.test")
    robot._client = httpx.AsyncClient(base_url="http://device-sim.test", transport=transport)
    server = build_robot_server(client=robot)
    try:
        payload = _payload(
            await server.call_tool(
                "create_delivery", {"order_no": "DEMO-MCP-1", "room_no": "3902", "item": "毛巾"}
            )
        )
        read_back = _payload(
            await server.call_tool("get_delivery", {"delivery_id": payload["delivery_id"]})
        )
    finally:
        await robot.aclose()
    assert payload["ok"] is True
    assert read_back["status"] == "queued"


@pytest.mark.asyncio
async def test_gateway_mounts_three_streamable_http_endpoints() -> None:
    gateway = build_app()
    assert [getattr(route, "path", None) for route in gateway.routes] == [
        "/healthz",
        "/lock",
        "/robot",
        "/wechat",
    ]

    transport = httpx.ASGITransport(app=gateway)
    async with gateway.router.lifespan_context(gateway):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://mcp-gateway.test"
        ) as http_client:
            for path, expected in (
                ("/lock/mcp", "issue_credential"),
                ("/robot/mcp", "create_delivery"),
                ("/wechat/mcp", "get_pay_order_status"),
            ):
                async with streamable_http_client(
                    f"http://mcp-gateway.test{path}", http_client=http_client
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert expected in names
