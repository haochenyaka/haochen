"""P1.4 记忆抽取规则单测：分类白名单、长度、敏感明文、去重与上限（不依赖 DB/模型）。"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.memory.extraction import (
    MemoryCandidate,
    NullMemoryExtractor,
    build_memory_extractor,
    validate_candidates,
)


def _candidate(category: str = "preference", content: str = "偏好高层安静房间") -> MemoryCandidate:
    return MemoryCandidate(category, content)


def test_validate_keeps_clean_candidates() -> None:
    assert validate_candidates([_candidate()], existing=set()) == [
        MemoryCandidate("preference", "偏好高层安静房间")
    ]


def test_validate_drops_unknown_category() -> None:
    assert validate_candidates([_candidate(category="secret")], existing=set()) == []


def test_validate_drops_sensitive_plaintext() -> None:
    assert validate_candidates([_candidate(content="联系电话 13812345678")], existing=set()) == []


def test_validate_drops_too_short_and_trims() -> None:
    assert validate_candidates([_candidate(content="好")], existing=set()) == []
    assert validate_candidates([_candidate(content="  偏好高层安静房间  ")], existing=set()) == [
        MemoryCandidate("preference", "偏好高层安静房间")
    ]


def test_validate_dedupes_existing_and_within_batch() -> None:
    existing = {("preference", "偏好高层安静房间")}
    batch = [_candidate(), _candidate(), _candidate(content="偏好无烟房")]
    assert validate_candidates(batch, existing=existing) == [
        MemoryCandidate("preference", "偏好无烟房")
    ]


def test_validate_caps_items(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "memory_extract_max_items", 2)
    batch = [_candidate(content=f"偏好编号{i}") for i in range(5)]
    assert len(validate_candidates(batch, existing=set())) == 2


def test_build_extractor_is_null_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "memory_extract_enabled", False)
    monkeypatch.setattr(get_settings(), "model_api_key", "secret")
    assert isinstance(build_memory_extractor(), NullMemoryExtractor)


def test_build_extractor_is_null_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "memory_extract_enabled", True)
    monkeypatch.setattr(get_settings(), "model_api_key", "")
    assert isinstance(build_memory_extractor(), NullMemoryExtractor)


def test_null_extractor_yields_no_candidates() -> None:
    import asyncio

    assert asyncio.run(NullMemoryExtractor().extract(transcript="客人：我喜欢高层")) == ()
