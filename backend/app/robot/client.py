"""机器人渠道客户端：配送下单与状态查询（HTTP + JSON，契约 §3）。

不变量：
- 非 2xx 抛 RobotError（含 404 未知配送）；连接/超时抛 RobotUnavailableError（未取得
  结果），调用方按可重试处理；
- 状态迁移由设备侧驱动，客户端只读回状态，不自行推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0


class RobotError(RuntimeError):
    """机器人返回明确失败（非 2xx 或响应体不合约）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RobotUnavailableError(RobotError):
    """机器人渠道不可达：未取得结果，按可重试处理。"""


@dataclass(frozen=True)
class RobotDelivery:
    delivery_id: str
    order_no: str
    room_no: str
    item: str
    status: str
    created_at: datetime
    updated_at: datetime


class RobotClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    @classmethod
    def from_settings(cls) -> RobotClient:
        from app.config import get_settings

        return cls(base_url=get_settings().device_sim_base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, *, json: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise RobotUnavailableError(f"机器人渠道不可达：{path}") from exc
        if response.status_code >= 400:
            raise RobotError(
                f"机器人渠道返回错误：{path} status={response.status_code}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RobotError(f"机器人渠道响应不是合法 JSON：{path}") from exc

    async def create_delivery(self, *, order_no: str, room_no: str, item: str) -> RobotDelivery:
        body = await self._request(
            "POST",
            "/robot/v1/deliveries",
            json={"order_no": order_no, "room_no": room_no, "item": item},
        )
        return _delivery(body)

    async def get_delivery(self, delivery_id: str) -> RobotDelivery:
        body = await self._request("GET", f"/robot/v1/deliveries/{delivery_id}")
        return _delivery(body)


def _delivery(body: Any) -> RobotDelivery:
    if not isinstance(body, dict):
        raise RobotError("机器人配送响应结构不合约")
    try:
        return RobotDelivery(
            delivery_id=str(body["delivery_id"]),
            order_no=str(body["order_no"]),
            room_no=str(body["room_no"]),
            item=str(body["item"]),
            status=str(body["status"]),
            created_at=datetime.fromisoformat(str(body["created_at"])),
            updated_at=datetime.fromisoformat(str(body["updated_at"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RobotError("机器人配送响应缺少必需字段或格式不合约") from exc
