"""第二段重排：把宽捞候选按与问题的相关度精排并截断。

不变量：
- 重排不新增候选、不改写候选正文，输出是召回集合的子集；
- 未配置模型时走确定性词面重排，本地与 CI 结果可复现；
- 重排故障只降级为召回顺序（调用方决定），不得让咨询链路整体不可用。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings
from app.knowledge.tokenization import tokenize

logger = logging.getLogger(__name__)

# 词面重排的截断长度：只影响打分速度，不改变候选正文
SCORE_EXCERPT_CHARS = 1200
LLM_EXCERPT_CHARS = 400
HTTP_RERANK_BATCH_SIZE = 32
JSON_FENCE = "```"


class RerankError(Exception):
    """重排供给不可用或返回结构异常。"""


@dataclass(frozen=True)
class RerankCandidate:
    chunk_id: str
    content: str
    title: str
    recall_score: float


class Reranker(Protocol):
    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate], *, top_n: int
    ) -> list[tuple[str, float]]:
        """返回 (chunk_id, score) 降序列表，长度为 min(top_n, len(candidates))。"""
        ...


class RecallOrderReranker:
    """保序重排：召回的 RRF 融合已是相关度序，第二段只做截断。"""

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate], *, top_n: int
    ) -> list[tuple[str, float]]:
        return [(item.chunk_id, item.recall_score) for item in candidates[:top_n]]


class KeywordReranker:
    """确定性词面重排：问题词元覆盖率为主，整串命中与召回序仅作区分度补充。"""

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate], *, top_n: int
    ) -> list[tuple[str, float]]:
        query_tokens = set(tokenize(query))
        needle = query.strip().lower()
        scored: list[tuple[str, float]] = []
        for rank, item in enumerate(candidates):
            excerpt = item.content[:SCORE_EXCERPT_CHARS]
            document_tokens = set(tokenize(excerpt)) | set(tokenize(item.title))
            overlap = len(query_tokens & document_tokens)
            coverage = overlap / len(query_tokens) if query_tokens else 0.0
            phrase_bonus = 0.3 if needle and needle in excerpt.lower() else 0.0
            # 召回序作为百万分之一量级的次序扰动：同分时保持上游宽捞的相对顺序
            order_tiebreak = 1e-6 * (len(candidates) - rank)
            scored.append((item.chunk_id, round(coverage + phrase_bonus + order_tiebreak, 9)))
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_n]


class LlmReranker:
    """模型重排：让对话模型给候选打 0-10 相关度分，解析失败即整体降级。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url or not model:
            raise RerankError("模型重排需要 MODEL_BASE_URL 与 MODEL_NAME")
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate], *, top_n: int
    ) -> list[tuple[str, float]]:
        if not candidates:
            return []
        numbered = [
            {
                "id": index + 1,
                "标题": item.title,
                "片段": item.content[:LLM_EXCERPT_CHARS],
            }
            for index, item in enumerate(candidates)
        ]
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是检索重排器。判断每个片段与用户问题的相关度，"
                        '输出 JSON：{"scores": [{"id": 1, "score": 0-10}]}，'
                        "不要输出解释。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps({"问题": query, "候选": numbered}, ensure_ascii=False),
                },
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise RerankError(f"重排模型调用失败：{exc}") from exc
        except ValueError as exc:
            raise RerankError(f"重排模型返回非 JSON：{exc}") from exc

        scores = _parse_scores(body, expected=len(candidates))
        order = sorted(range(len(candidates)), key=lambda index: scores[index], reverse=True)
        return [(candidates[index].chunk_id, scores[index]) for index in order[:top_n]]


class HttpReranker:
    """cross-encoder 重排：POST /rerank，TEI 兼容响应为 [{index, score}]。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        timeout_seconds: float = 30.0,
        batch_size: int = HTTP_RERANK_BATCH_SIZE,
    ) -> None:
        if not base_url:
            raise RerankError("HTTP 重排需要 RERANK_BASE_URL")
        if batch_size <= 0:
            raise RerankError("HTTP 重排批次大小必须为正数")
        self._endpoint = base_url.rstrip("/") + "/rerank"
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._batch_size = batch_size

    async def rerank(
        self, query: str, candidates: Sequence[RerankCandidate], *, top_n: int
    ) -> list[tuple[str, float]]:
        if not candidates:
            return []
        scored: list[tuple[int, float]] = []
        for start in range(0, len(candidates), self._batch_size):
            batch = candidates[start : start + self._batch_size]
            scores = await self._score(query, batch)
            scored.extend((start + offset, score) for offset, score in enumerate(scores))
        order = sorted(scored, key=lambda pair: pair[1], reverse=True)
        return [(candidates[index].chunk_id, score) for index, score in order[:top_n]]

    async def _score(self, query: str, candidates: Sequence[RerankCandidate]) -> list[float]:
        payload = {
            "query": query,
            "texts": [item.content for item in candidates],
            "raw_scores": False,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise RerankError(f"重排接口调用失败：{exc}") from exc
        except ValueError as exc:
            raise RerankError(f"重排接口返回非 JSON：{exc}") from exc
        return _parse_http_scores(body, expected=len(candidates))


def _parse_http_scores(body: object, *, expected: int) -> list[float]:
    if not isinstance(body, list) or len(body) != expected:
        raise RerankError("重排接口返回条数与候选不一致")
    scores = [0.0] * expected
    seen: set[int] = set()
    for item in body:
        if not isinstance(item, dict):
            raise RerankError("重排接口返回结构异常：条目不是对象")
        index = item.get("index")
        score = item.get("score")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected:
            raise RerankError("重排接口返回结构异常：index 越界")
        if index in seen:
            raise RerankError("重排接口返回结构异常：index 重复")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise RerankError("重排接口返回结构异常：score 非数值")
        seen.add(index)
        scores[index] = float(score)
    if len(seen) != expected:
        raise RerankError("重排接口返回条数与候选不一致")
    return scores


def _parse_scores(body: object, *, expected: int) -> list[float]:
    content = _message_content(body)
    payload = _load_json_object(content)
    items = payload.get("scores")
    if not isinstance(items, list) or len(items) != expected:
        raise RerankError("重排模型返回条数与候选不一致")
    scores = [0.0] * expected
    for item in items:
        if not isinstance(item, dict):
            raise RerankError("重排模型返回结构异常：条目不是对象")
        position = item.get("id")
        score = item.get("score")
        if not isinstance(position, int) or not 1 <= position <= expected:
            raise RerankError("重排模型返回结构异常：id 越界")
        if not isinstance(score, (int, float)):
            raise RerankError("重排模型返回结构异常：score 非数值")
        scores[position - 1] = float(score)
    return scores


def _message_content(body: object) -> str:
    if not isinstance(body, dict):
        raise RerankError("重排模型返回结构异常：缺少 choices")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RerankError("重排模型返回结构异常：choices 为空")
    first = choices[0]
    if not isinstance(first, dict):
        raise RerankError("重排模型返回结构异常：choices 条目不是对象")
    message = first.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RerankError("重排模型返回结构异常：message.content 缺失")
    return str(message["content"])


def _load_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith(JSON_FENCE):
        text = text.strip("`")
        _, _, text = text.partition("\n")
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise RerankError(f"重排模型返回无法解析为 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise RerankError("重排模型返回结构异常：不是 JSON 对象")
    return parsed


def build_reranker() -> Reranker:
    """按配置选择重排实现：本地 cross-encoder 优先，其次对话模型，最后词面。"""
    settings = get_settings()
    if settings.kb_rerank_enabled and settings.rerank_base_url:
        return HttpReranker(
            base_url=settings.rerank_base_url,
            api_key=settings.rerank_api_key,
            timeout_seconds=settings.kb_rerank_timeout_seconds,
        )
    if settings.kb_rerank_enabled and settings.model_name and settings.model_base_url:
        return LlmReranker(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key,
            model=settings.model_name,
            timeout_seconds=settings.kb_rerank_timeout_seconds,
        )
    return KeywordReranker()
