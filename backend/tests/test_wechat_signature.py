"""回调验签单元测试：契约 §1.5 的规范化序列化与失败关闭。"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from app.wechat import canonical_signature, verify_callback_signature

SECRET = "test-signing-secret"


def _contract_sign(body: dict[str, Any], *, ensure_ascii: bool = False) -> str:
    """按契约 §1.5 独立实现：规范化 JSON（UTF-8）+ HMAC-SHA256。"""
    raw = json.dumps(body, sort_keys=True, ensure_ascii=ensure_ascii)
    return hmac.new(SECRET.encode(), raw.encode("utf-8"), hashlib.sha256).hexdigest()


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "event": "success",
        "order_no": "H20260928ABCDEF",
        "transaction_id": "txn_0123456789abcdef",
        "amount_fen": 79600,
        "openid": "openid_abc",
        "notified_at": "2026-09-28T08:00:00+08:00",
    }
    body.update(overrides)
    return body


def test_signature_matches_contract_serialization() -> None:
    body = _body()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert canonical_signature(raw, secret=SECRET) == _contract_sign(body)
    # 键序不影响（发送端可以不排序，验签端自行规范化）
    shuffled = json.dumps({key: body[key] for key in reversed(list(body))}, ensure_ascii=False)
    assert canonical_signature(shuffled.encode("utf-8"), secret=SECRET) == _contract_sign(body)


def test_signature_preserves_non_ascii() -> None:
    body = _body(order_no="H皓晨0001")
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    assert canonical_signature(raw, secret=SECRET) == _contract_sign(body)
    # ensure_ascii=True 的序列化不是契约口径，不得误判为通过
    assert canonical_signature(raw, secret=SECRET) != _contract_sign(body, ensure_ascii=True)


def test_verify_accepts_only_matching_signature() -> None:
    body = _body()
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    signature = _contract_sign(body)
    assert verify_callback_signature(raw, signature, secret=SECRET) is True
    assert verify_callback_signature(raw, signature.upper(), secret=SECRET) is False
    assert verify_callback_signature(raw, "0" * 64, secret=SECRET) is False
    assert verify_callback_signature(raw, None, secret=SECRET) is False
    assert verify_callback_signature(raw, "", secret=SECRET) is False
    assert verify_callback_signature(raw, signature, secret="other-secret") is False


def test_verify_fails_closed_on_malformed_body() -> None:
    assert canonical_signature(b"{not-json", secret=SECRET) is None
    assert canonical_signature(b"[1, 2, 3]", secret=SECRET) is None
    assert canonical_signature(b"\xff\xfe", secret=SECRET) is None
    assert verify_callback_signature(b"{not-json", "0" * 64, secret=SECRET) is False


def test_tampered_body_fails_verification() -> None:
    body = _body()
    signature = _contract_sign(body)
    tampered = json.dumps(_body(amount_fen=1), ensure_ascii=False).encode("utf-8")
    assert verify_callback_signature(tampered, signature, secret=SECRET) is False
