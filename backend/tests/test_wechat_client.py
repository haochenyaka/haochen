"""微信渠道客户端传输层测试：httpx.MockTransport 覆盖签约字段搬运与失败分类。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.wechat import SubscribeMessage, WechatClient, WechatError, WechatUnavailableError

PAYLOAD = {
    "openid": "openid_1",
    "template_id": "tmpl-checkin-v1",
    "page": "pages/orders/index",
    "data": {"room_no": "8301", "floor": "8", "code": "123456", "guide": "退房 12:00 前"},
}


def _client(handler: httpx.MockTransport) -> WechatClient:
    client = WechatClient(base_url="http://wechat.test")
    client._client = httpx.AsyncClient(  # noqa: SLF001  传输层替身只替换连接实现
        base_url="http://wechat.test", transport=handler
    )
    return client


def _handler(response: httpx.Response) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: response)


def test_send_subscribe_message_moves_semantic_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message_id": "msg-1", "status": "sent"})

    async def _run() -> SubscribeMessage:
        client = _client(httpx.MockTransport(handler))
        try:
            return await client.send_subscribe_message(**PAYLOAD)  # type: ignore[arg-type]
        finally:
            await client.aclose()

    import asyncio

    sent = asyncio.run(_run())
    assert sent == SubscribeMessage(message_id="msg-1", status="sent")
    assert captured["path"] == "/wechat/v1/notify/subscribe"
    assert captured["body"] == PAYLOAD


def test_send_subscribe_message_requires_message_id() -> None:
    async def _run() -> None:
        client = _client(_handler(httpx.Response(200, json={"status": "sent"})))
        try:
            await client.send_subscribe_message(**PAYLOAD)  # type: ignore[arg-type]
        finally:
            await client.aclose()

    import asyncio

    with pytest.raises(WechatError, match="message_id"):
        asyncio.run(_run())


def test_send_subscribe_message_classifies_channel_failures() -> None:
    async def _rejected() -> None:
        client = _client(_handler(httpx.Response(503, json={"error": "injected_fault"})))
        try:
            await client.send_subscribe_message(**PAYLOAD)  # type: ignore[arg-type]
        finally:
            await client.aclose()

    async def _unreachable() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = _client(httpx.MockTransport(handler))
        try:
            await client.send_subscribe_message(**PAYLOAD)  # type: ignore[arg-type]
        finally:
            await client.aclose()

    import asyncio

    with pytest.raises(WechatError, match="status=503") as rejected:
        asyncio.run(_rejected())
    assert not isinstance(rejected.value, WechatUnavailableError)

    with pytest.raises(WechatUnavailableError, match="不可达"):
        asyncio.run(_unreachable())
