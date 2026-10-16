"""敏感明文打码：密码整值占位；身份证/手机号保留前后缀、中间等长打码。"""

from __future__ import annotations

from app.agents.middlewares.credential_guard import (
    MASK_TEXT,
    CredentialGuardStation,
    mask_sensitive_plaintext,
    scan_sensitive_plaintext,
)

PHONE = "13812345678"
ID_CARD = "110101199001011234"


def test_phone_keeps_prefix_and_suffix_with_masked_middle() -> None:
    masked, used = mask_sensitive_plaintext(f"联系电话：{PHONE}")
    assert masked == "联系电话：138****5678"
    assert used == ("phone",)


def test_id_card_keeps_prefix_and_suffix_with_masked_middle() -> None:
    masked, used = mask_sensitive_plaintext(f"证件号 {ID_CARD} 已登记")
    assert masked == "证件号 110101********1234 已登记"
    assert used == ("id_card",)


def test_password_uses_whole_placeholder() -> None:
    masked, used = mask_sensitive_plaintext("门锁密码：839201")
    assert masked == f"门锁密码：{MASK_TEXT}"
    assert used == ("password",)


def test_masked_forms_no_longer_scan_as_plaintext() -> None:
    # 扫描器仍按完整形态判定；打码破坏完整形态后必须零命中
    assert scan_sensitive_plaintext(PHONE)
    assert scan_sensitive_plaintext(ID_CARD)
    phone_masked, _ = mask_sensitive_plaintext(PHONE)
    id_masked, _ = mask_sensitive_plaintext(ID_CARD)
    assert scan_sensitive_plaintext(phone_masked) == []
    assert scan_sensitive_plaintext(id_masked) == []


def test_non_sensitive_numbers_are_untouched() -> None:
    # 订单号（20 位纯数字）既非身份证也非手机号，不得误伤
    order_no = "12345678901234567890"
    masked, used = mask_sensitive_plaintext(f"订单号 {order_no}")
    assert masked == f"订单号 {order_no}"
    assert used == ()


def test_only_requested_categories_are_masked() -> None:
    masked, used = mask_sensitive_plaintext(f"{PHONE} {ID_CARD}", categories=("phone",))
    assert masked == f"138****5678 {ID_CARD}"
    assert used == ("phone",)


def test_multiple_hits_in_one_text() -> None:
    text = f"手机 {PHONE} 备用 {PHONE} 证件 {ID_CARD}"
    masked, used = mask_sensitive_plaintext(text)
    assert masked == "手机 138****5678 备用 138****5678 证件 110101********1234"
    assert sorted(used) == ["id_card", "phone", "phone"]


def test_station_default_masks_phone_and_id_card_in_tool_result() -> None:
    """工位出厂默认即打码三类：工具入参审计副本里的手机号/身份证被打成部分形态。"""
    station = CredentialGuardStation()
    args: dict[str, object] = {"phone": PHONE, "note": f"身份证 {ID_CARD}"}
    station.redact_tool_args(args)
    assert args["phone"] == "138****5678"
    assert args["note"] == "身份证 110101********1234"
