"""机器人 / 微信渠道客户端 ↔ 设备模拟器契约测试（ASGI 直连真实 mock）。"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.device_sim.main import app as sim_app
from app.robot.client import RobotClient, RobotError
from app.wechat.client import WechatClient, WechatError

BASE_URL = "http://channel.test"


def test_robot_delivery_create_and_read() -> None:
    async def _run() -> None:
        transport = httpx.ASGITransport(app=sim_app)
        client = RobotClient(base_url=BASE_URL)
        client._client = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
        try:
            created = await client.create_delivery(
                order_no="DEMO-ROBOT-1", room_no="8301", item="矿泉水"
            )
            assert created.status == "queued"
            assert created.room_no == "8301"

            read_back = await client.get_delivery(created.delivery_id)
            assert read_back.delivery_id == created.delivery_id
            assert read_back.item == "矿泉水"

            with pytest.raises(RobotError) as missing:
                await client.get_delivery("dev_missing")
            assert missing.value.status_code == 404
        finally:
            await client.aclose()

    asyncio.run(_run())


def test_wechat_pay_order_status_read() -> None:
    async def _run() -> None:
        transport = httpx.ASGITransport(app=sim_app)
        client = WechatClient(base_url=BASE_URL)
        client._client = httpx.AsyncClient(base_url=BASE_URL, transport=transport)
        try:
            created = await client.create_pay_order(
                order_no="DEMO-PAY-1", amount_fen=119400, openid="openid_demo"
            )
            assert created.prepay_id.startswith("prepay_")

            status = await client.get_pay_order("DEMO-PAY-1")
            assert status.order_no == "DEMO-PAY-1"
            assert status.amount_fen == 119400
            assert status.status == "created"
            assert status.notifications == 0

            with pytest.raises(WechatError):
                await client.get_pay_order("DEMO-PAY-MISSING")
        finally:
            await client.aclose()

    asyncio.run(_run())
