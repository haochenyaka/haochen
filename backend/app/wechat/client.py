"""微信渠道客户端：code2session、JSAPI 下单、支付单查询与订阅消息下发（HTTP + JSON，契约 §4）。

不变量：
- 非 2xx 或响应体缺必需字段抛 WechatError（明确失败）；连接/超时抛
  WechatUnavailableError（未取得结果），调用方按"可重试"处理；
- 客户端无状态、不做重试：重试语义由调用方与幂等键决定；
- 不落任何明文支付凭证，prepay_id 与小程序支付参数原样透传；
- 订阅消息载荷里的开门明文只随本次请求发往渠道，客户端不打印、不缓存。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0


class WechatError(RuntimeError):
    """渠道返回了明确失败结果（非 2xx、响应体不合约或缺少必需字段）。"""


class WechatUnavailableError(WechatError):
    """渠道不可达（连接/超时/传输错误）：未取得结果，可安全重试。"""


@dataclass(frozen=True)
class WechatSession:
    openid: str
    session_key: str


@dataclass(frozen=True)
class PrepayOrder:
    prepay_id: str
    pay_params: dict[str, Any]


@dataclass(frozen=True)
class SubscribeMessage:
    message_id: str
    status: str


class WechatClient:
    def __init__(self, *, base_url: str, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_seconds)

    @classmethod
    def from_settings(cls) -> WechatClient:
        from app.config import get_settings

        settings = get_settings()
        return cls(base_url=settings.device_sim_base_url)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(path, json=payload)
        except httpx.HTTPError as exc:
            raise WechatUnavailableError(f"微信渠道不可达：{path}") from exc
        if response.status_code >= 400:
            raise WechatError(f"微信渠道返回错误：{path} status={response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WechatError(f"微信渠道响应不是合法 JSON：{path}") from exc
        if not isinstance(body, dict):
            raise WechatError(f"微信渠道响应结构不合约：{path}")
        return body

    async def code2session(self, code: str) -> WechatSession:
        body = await self._post("/wechat/v1/login", {"code": code})
        openid = body.get("openid")
        session_key = body.get("session_key")
        if not isinstance(openid, str) or not openid or not isinstance(session_key, str):
            raise WechatError("微信登录响应缺少 openid/session_key")
        return WechatSession(openid=openid, session_key=session_key)

    async def create_pay_order(
        self,
        *,
        order_no: str,
        amount_fen: int,
        openid: str,
        description: str = "房费",
    ) -> PrepayOrder:
        body = await self._post(
            "/wechat/v1/pay/orders",
            {
                "order_no": order_no,
                "amount_fen": amount_fen,
                "openid": openid,
                "description": description,
            },
        )
        prepay_id = body.get("prepay_id")
        pay_params = body.get("pay_params")
        if not isinstance(prepay_id, str) or not prepay_id or not isinstance(pay_params, dict):
            raise WechatError("微信下单响应缺少 prepay_id/pay_params")
        return PrepayOrder(prepay_id=prepay_id, pay_params=dict(pay_params))

    async def send_subscribe_message(
        self,
        *,
        openid: str,
        template_id: str,
        data: Mapping[str, str],
        page: str | None = None,
    ) -> SubscribeMessage:
        """下发订阅消息（服务通知）：字段键为语义键，模板字段映射由传输层适配（契约 §4.4）。"""
        body = await self._post(
            "/wechat/v1/notify/subscribe",
            {
                "openid": openid,
                "template_id": template_id,
                "page": page,
                "data": dict(data),
            },
        )
        message_id = body.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise WechatError("订阅消息响应缺少 message_id")
        status = body.get("status")
        return SubscribeMessage(
            message_id=message_id, status=str(status) if isinstance(status, str) else "sent"
        )
