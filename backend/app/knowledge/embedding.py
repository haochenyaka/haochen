"""嵌入供给：OpenAI 兼容接口为生产实现，确定性实现供开发与测试。

不变量：
- 未配置 `MODEL_EMBEDDING_NAME` 时回退到确定性实现（本地/CI 可离线跑通全链路）；
- 嵌入向量维度必须全collection一致：维度由当前供给第一次产出后固化到集合 schema；
- 批量调用失败直接抛出，由任务层收敛为 error_indexing（不静默降级为零向量）。
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Protocol

import httpx

from app.config import get_settings
from app.knowledge.tokenization import tokenize

DEV_EMBEDDING_DIMENSIONS = 64


class EmbeddingError(Exception):
    """嵌入供给不可用或返回结构异常。"""


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> str:
        """供给身份：用于集合命名，维度不同的供给不共享集合。"""
        ...

    @property
    def dimensions(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddings:
    """开发与测试用：按 token 哈希投影到固定维度，保证同文本同向量、可离线复现。"""

    def __init__(self, *, dimensions: int = DEV_EMBEDDING_DIMENSIONS) -> None:
        self._dimensions = dimensions

    @property
    def identity(self) -> str:
        return "dev"

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dimensions
        tokens = tokenize(text)
        if not tokens:
            vector[0] = 1.0
            return vector
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = struct.unpack("<I", digest[:4])[0] % self._dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


class OpenAICompatibleEmbeddings:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int = 0,
        timeout_seconds: float = 60.0,
    ) -> None:
        if not base_url or not model:
            raise EmbeddingError(
                "嵌入供给需要 EMBEDDING_BASE_URL（或 MODEL_BASE_URL）与 MODEL_EMBEDDING_NAME"
            )
        # 约定 base_url 与 OpenAI 一致已包含 /v1；TEI 的兼容端点即 /v1/embeddings
        self._endpoint = base_url.rstrip("/") + "/embeddings"
        self._api_key = api_key
        self._model = model
        self._dimensions = dimensions
        self._timeout = timeout_seconds

    @property
    def identity(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict[str, object] = {"model": self._model, "input": texts}
        if self._dimensions:
            payload["dimensions"] = self._dimensions
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._endpoint, json=payload, headers=headers)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"嵌入接口调用失败：{exc}") from exc
        except ValueError as exc:
            raise EmbeddingError(f"嵌入接口返回非 JSON：{exc}") from exc

        vectors = _parse_embeddings(body, expected=len(texts))
        if self._dimensions == 0:
            self._dimensions = len(vectors[0]) if vectors else 0
        return vectors


def _parse_embeddings(body: object, *, expected: int) -> list[list[float]]:
    if not isinstance(body, dict):
        raise EmbeddingError("嵌入接口返回结构异常：缺少 data 字段")
    items = body.get("data")
    if not isinstance(items, list) or len(items) != expected:
        raise EmbeddingError("嵌入接口返回条数与请求不一致")
    ordered: list[tuple[int, list[float]]] = []
    for position, item in enumerate(items):
        if not isinstance(item, dict):
            raise EmbeddingError("嵌入接口返回结构异常：条目不是对象")
        index = item.get("index", position)
        embedding = item.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise EmbeddingError("嵌入接口返回结构异常：embedding 为空")
        ordered.append((int(index), [float(value) for value in embedding]))
    ordered.sort(key=lambda pair: pair[0])
    return [vector for _, vector in ordered]


def build_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if not settings.model_embedding_name:
        return DeterministicEmbeddings()
    # 独立 embedding endpoint 激活后不再借用 chat 的密钥，避免把对话密钥发给本地服务
    if settings.embedding_base_url:
        base_url = settings.embedding_base_url
        api_key = settings.embedding_api_key
    else:
        base_url = settings.model_base_url
        api_key = settings.model_api_key
    return OpenAICompatibleEmbeddings(
        base_url=base_url,
        api_key=api_key,
        model=settings.model_embedding_name,
        dimensions=settings.embedding_dimensions,
        timeout_seconds=settings.model_timeout_seconds,
    )
