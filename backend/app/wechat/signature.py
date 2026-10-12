"""回调验签：规范化 JSON 序列化 + HMAC-SHA256（契约 §1.5）。

规范化：`json.dumps(body, sort_keys=True, ensure_ascii=False)` 的 UTF-8 字节；
摘要与头部值用 `hmac.compare_digest` 恒定时间比较，避免时序侧信道。
"""

from __future__ import annotations

import hashlib
import hmac
import json


def canonical_signature(body: bytes, *, secret: str) -> str | None:
    """按契约计算回调摘要；体不是合法 UTF-8 JSON 对象时返回 None（验签必失败）。"""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hmac.new(secret.encode(), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_callback_signature(body: bytes, signature: str | None, *, secret: str) -> bool:
    if not signature:
        return False
    expected = canonical_signature(body, secret=secret)
    if expected is None:
        return False
    return hmac.compare_digest(expected, signature)
