"""隐私告知与 PII 存储边界守卫。"""

from __future__ import annotations

from app.config import Settings
from app.models import Base
from app.services.consent_service import (
    CONSENT_PURPOSES,
    build_notice,
)

# 身份/证件/人脸原图与向量一律不得以明文字段落库：核验只存结果与必要摘要。
FORBIDDEN_PII_COLUMNS = frozenset(
    {
        "id_card",
        "id_card_no",
        "id_card_number",
        "id_number",
        "id_card_image",
        "id_card_front",
        "id_card_back",
        "passport_no",
        "passport_image",
        "face_image",
        "face_embedding",
        "face_vector",
        "face_photo",
    }
)


def test_notice_exposes_version_purposes_and_cross_border_policy() -> None:
    settings = Settings(env="local", privacy_policy_version="2026-10-18")
    notice = build_notice(settings)

    assert notice.policy_version == "2026-10-18"
    assert {item.purpose for item in notice.purposes} == {item.purpose for item in CONSENT_PURPOSES}
    assert any(item.purpose == "privacy_policy" and item.required for item in notice.purposes)
    assert any(item.purpose == "face_recognition" for item in notice.purposes)
    assert notice.data_categories
    assert notice.retention
    assert "境外" in notice.cross_border


def test_consent_records_table_registered() -> None:
    assert "consent_records" in Base.metadata.tables
    columns = set(Base.metadata.tables["consent_records"].columns.keys())
    assert {"user_id", "purpose", "policy_version", "granted", "created_at"} <= columns


def test_no_plaintext_identity_or_face_columns() -> None:
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if column.name in FORBIDDEN_PII_COLUMNS:
                offenders.append(f"{table.name}.{column.name}")
    assert offenders == []
