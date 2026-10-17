"""嵌入供给单元测试：确定性实现的稳定性与 OpenAI 兼容响应解析。"""

from __future__ import annotations

import asyncio
import math

import pytest

from app.config import get_settings
from app.knowledge.embedding import (
    DeterministicEmbeddings,
    EmbeddingError,
    OpenAICompatibleEmbeddings,
    _parse_embeddings,
    build_embedding_provider,
)


def test_deterministic_embeddings_are_stable_and_normalized() -> None:
    provider = DeterministicEmbeddings(dimensions=32)
    first = asyncio.run(provider.embed(["早餐几点开始？", "早餐几点开始？", "早餐在哪里吃"]))
    assert first[0] == first[1]
    assert first[0] != first[2]
    assert len(first[0]) == 32
    assert math.isclose(sum(value * value for value in first[0]), 1.0, rel_tol=1e-6)


def test_deterministic_embeddings_handle_empty_input() -> None:
    provider = DeterministicEmbeddings()
    assert provider.dimensions == 64
    assert asyncio.run(provider.embed([])) == []


def test_parse_embeddings_sorts_by_index_and_validates_shape() -> None:
    parsed = _parse_embeddings(
        {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        },
        expected=2,
    )
    assert parsed == [[1.0, 0.0], [0.0, 1.0]]

    with pytest.raises(EmbeddingError):
        _parse_embeddings({"data": [{"index": 0, "embedding": [1.0]}]}, expected=2)
    with pytest.raises(EmbeddingError):
        _parse_embeddings({"data": [{"index": 0, "embedding": []}]}, expected=1)
    with pytest.raises(EmbeddingError):
        _parse_embeddings([], expected=1)


def test_openai_compatible_embeddings_requires_endpoint_configuration() -> None:
    with pytest.raises(EmbeddingError):
        OpenAICompatibleEmbeddings(base_url="", api_key="k", model="m")
    with pytest.raises(EmbeddingError):
        OpenAICompatibleEmbeddings(base_url="http://localhost:1/v1", api_key="k", model="")


def test_build_embedding_provider_falls_back_to_dev_without_model_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "model_embedding_name", "")
    assert isinstance(build_embedding_provider(), DeterministicEmbeddings)

    monkeypatch.setattr(get_settings(), "model_embedding_name", "text-embedding-3-small")
    monkeypatch.setattr(get_settings(), "embedding_base_url", "")
    monkeypatch.setattr(get_settings(), "embedding_api_key", "")
    monkeypatch.setattr(get_settings(), "model_base_url", "http://localhost:9999/v1")
    provider = build_embedding_provider()
    assert isinstance(provider, OpenAICompatibleEmbeddings)
    assert provider.identity == "text-embedding-3-small"
    assert provider._endpoint == "http://localhost:9999/v1/embeddings"
    assert provider._api_key == get_settings().model_api_key


def test_build_embedding_provider_prefers_dedicated_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "model_embedding_name", "bge-m3")
    monkeypatch.setattr(settings, "embedding_dimensions", 1024)
    monkeypatch.setattr(settings, "embedding_base_url", "http://bge-embed:80/v1")
    monkeypatch.setattr(settings, "embedding_api_key", "embed-key")
    monkeypatch.setattr(settings, "model_base_url", "https://api.deepseek.com/v1")
    monkeypatch.setattr(settings, "model_api_key", "chat-key")

    provider = build_embedding_provider()

    assert isinstance(provider, OpenAICompatibleEmbeddings)
    assert provider.identity == "bge-m3"
    assert provider.dimensions == 1024
    assert provider._endpoint == "http://bge-embed:80/v1/embeddings"
    assert provider._api_key == "embed-key"
