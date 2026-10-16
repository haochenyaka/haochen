"""config_json 归一化：嵌套 `{context, middleware}` 与旧扁平形状都要读得出来。"""

from __future__ import annotations

import pytest

from app.services.agent_config_service import normalize_config_json


def test_flat_config_is_treated_as_context() -> None:
    context, middleware = normalize_config_json({"system_prompt": "hi", "temperature": 0.3})
    assert context == {"system_prompt": "hi", "temperature": 0.3}
    assert middleware == {}


def test_nested_config_is_split() -> None:
    context, middleware = normalize_config_json(
        {
            "context": {"system_prompt": "hi"},
            "middleware": {"summarization": {"threshold_tokens": 5}},
        }
    )
    assert context == {"system_prompt": "hi"}
    assert middleware == {"summarization": {"threshold_tokens": 5}}


def test_none_yields_empty_sections() -> None:
    assert normalize_config_json(None) == ({}, {})


def test_non_object_section_fails_explicitly() -> None:
    with pytest.raises(ValueError):
        normalize_config_json({"context": "not-an-object"})
    with pytest.raises(ValueError):
        normalize_config_json({"middleware": ["not", "an", "object"]})
