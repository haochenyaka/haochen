"""微信渠道接入：登录换取身份、JSAPI 下单、回调验签与订阅消息下发。

约定：
- 客户端只做契约字段搬运，不落明文支付凭证；渠道失败一律显式抛出，不静默降级；
- 回调验签是本包唯一入口（`verify_callback_signature`），路由处失败即 401、不写事实；
- 订阅消息载荷携带的开门明文只随请求离开进程，不落库、不打印。
"""

from app.wechat.client import (
    PrepayOrder,
    SubscribeMessage,
    WechatClient,
    WechatError,
    WechatSession,
    WechatUnavailableError,
)
from app.wechat.signature import canonical_signature, verify_callback_signature

__all__ = [
    "PrepayOrder",
    "SubscribeMessage",
    "WechatClient",
    "WechatError",
    "WechatSession",
    "WechatUnavailableError",
    "canonical_signature",
    "verify_callback_signature",
]
