"""重排与检索组装单元测试：确定性词面重排、模型重排解析、参数与降级边界。"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.config import get_settings
from app.knowledge import rerank as rerank_module
from app.knowledge.rerank import (
    KeywordReranker,
    LlmReranker,
    RecallOrderReranker,
    RerankCandidate,
    RerankError,
    build_reranker,
)


def _candidate(chunk_id: str, content: str, *, title: str = "样例文档", score: float = 0.5):
    return RerankCandidate(chunk_id=chunk_id, content=content, title=title, recall_score=score)


def _run(coroutine):
    return asyncio.run(coroutine)


def test_keyword_reranker_orders_by_query_coverage() -> None:
    candidates = [
        _candidate("c1", "客房 WiFi 名称为 HaoChen-Guest，密码见房卡套。"),
        _candidate("c2", "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅。"),
        _candidate("c3", "洗衣服务 24 小时开放，费用记入房账。"),
    ]
    ranked = _run(KeywordReranker().rerank("早餐几点开始", candidates, top_n=3))
    assert ranked[0][0] == "c2"
    assert len(ranked) == 3
    assert [score for _, score in ranked] == sorted((score for _, score in ranked), reverse=True)


def test_keyword_reranker_keeps_recall_order_when_scores_tie() -> None:
    candidates = [
        _candidate("first", "退房时间是中午 12:00 前。", score=0.9),
        _candidate("second", "退房时间是中午 12:00 前，延迟退房需前台确认。", score=0.8),
    ]
    ranked = _run(KeywordReranker().rerank("退房时间", candidates, top_n=2))
    assert [chunk_id for chunk_id, _ in ranked][0] == "first"


def test_keyword_reranker_truncates_to_top_n_and_empty_query() -> None:
    candidates = [_candidate(f"c{index}", f"第 {index} 段内容") for index in range(5)]
    ranked = _run(KeywordReranker().rerank("内容", candidates, top_n=2))
    assert [chunk_id for chunk_id, _ in ranked] == ["c0", "c1"]
    blank = _run(KeywordReranker().rerank("   ", candidates, top_n=2))
    assert [chunk_id for chunk_id, _ in blank] == ["c0", "c1"]


def test_recall_order_reranker_truncates_without_reordering() -> None:
    candidates = [_candidate(f"c{index}", "内容", score=1.0 - index / 10) for index in range(4)]
    ranked = _run(RecallOrderReranker().rerank("问题", candidates, top_n=2))
    assert ranked == [("c0", 1.0), ("c1", 0.9)]


class _StubResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _StubClient:
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs: object) -> None:
        self._kwargs = kwargs

    async def __aenter__(self) -> _StubClient:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> _StubResponse:
        _StubClient.calls.append({"url": url, **kwargs})
        return _StubResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"scores": [{"id": 1, "score": 3}, {"id": 2, "score": 9}]}
                            )
                        }
                    }
                ]
            }
        )


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _StubClient.calls = []
    monkeypatch.setattr(rerank_module.httpx, "AsyncClient", _StubClient)


def test_llm_reranker_sorts_by_model_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)
    reranker = LlmReranker(base_url="http://model.local/v1", api_key="k", model="chat-x")
    candidates = [_candidate("low", "无关片段"), _candidate("high", "相关片段")]
    ranked = _run(reranker.rerank("早餐几点", candidates, top_n=2))
    assert ranked == [("high", 9.0), ("low", 3.0)]
    assert _StubClient.calls[0]["url"] == "http://model.local/v1/chat/completions"


def test_llm_reranker_rejects_malformed_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_client(monkeypatch)

    class _BrokenClient(_StubClient):
        async def post(self, url: str, **kwargs: object) -> _StubResponse:
            return _StubResponse({"choices": [{"message": {"content": "不是 JSON"}}]})

    monkeypatch.setattr(rerank_module.httpx, "AsyncClient", _BrokenClient)
    reranker = LlmReranker(base_url="http://model.local/v1", api_key="", model="chat-x")
    with pytest.raises(RerankError, match="无法解析"):
        _run(reranker.rerank("问题", [_candidate("c1", "内容")], top_n=1))


def test_llm_reranker_requires_endpoint() -> None:
    with pytest.raises(RerankError, match="MODEL_BASE_URL"):
        LlmReranker(base_url="", api_key="", model="chat-x")


def test_build_reranker_follows_settings_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_rerank_enabled", False)
    monkeypatch.setattr(settings, "model_name", "chat-x")
    monkeypatch.setattr(settings, "model_base_url", "http://model.local/v1")
    assert isinstance(build_reranker(), KeywordReranker)

    monkeypatch.setattr(settings, "kb_rerank_enabled", True)
    assert isinstance(build_reranker(), LlmReranker)

    monkeypatch.setattr(settings, "model_base_url", "")
    assert isinstance(build_reranker(), KeywordReranker)
