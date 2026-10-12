"""Milvus 分块集合：向量与 BM25 稀疏字段的读写与检索。

存储分工（机制基线 §2.8）：Milvus 只存检索所需字段（向量、BM25 字段、最小溯源键），
分块正文以 PostgreSQL 的 kb_chunks 为准；双写失败由调用方补偿为 error_indexing。

不变量：
- 集合按嵌入供给命名（`{prefix}_{identity}`），维度写入 schema 后不可变；
- BM25 稀疏向量由集合内建 Function 从 text 字段生成，写入方不提供；
- 删除按 document_id 过滤，重建索引先删后写不会残留旧块。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pymilvus import (
    AnnSearchRequest,
    DataType,
    Function,
    FunctionType,
    MilvusClient,
    RRFRanker,
)

from app.config import get_settings

logger = logging.getLogger(__name__)

TEXT_MAX_LENGTH = 6000
PRIMARY_MAX_LENGTH = 64
KEY_MAX_LENGTH = 64
DEFAULT_VECTOR_FIELD = "dense"
SPARSE_FIELD = "sparse"
TEXT_FIELD = "text"

_SLUG = re.compile(r"[^a-z0-9_]+")


class VectorStoreError(Exception):
    """向量库不可用或 schema 不匹配。"""


@dataclass(frozen=True)
class ChunkVector:
    chunk_id: str
    kb_id: str
    document_id: str
    ordinal: int
    text: str
    dense: list[float]


def collection_name_for(identity: str) -> str:
    prefix = get_settings().milvus_collection_prefix
    slug = _SLUG.sub("_", identity.lower()).strip("_") or "default"
    return f"{prefix}_{slug}"[:200]


class ChunkVectorStore:
    def __init__(self, client: MilvusClient, *, collection: str, dimensions: int = 0) -> None:
        self._client = client
        self._collection = collection
        self._dimensions = dimensions

    @classmethod
    def create(cls, *, identity: str, dimensions: int = 0) -> ChunkVectorStore:
        settings = get_settings()
        try:
            client = MilvusClient(uri=settings.milvus_uri)
        except Exception as exc:  # pragma: no cover - 连接失败路径由调用方收敛
            raise VectorStoreError(f"Milvus 不可达：{exc}") from exc
        return cls(client, collection=collection_name_for(identity), dimensions=dimensions)

    @property
    def collection(self) -> str:
        return self._collection

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def ensure_collection(self, *, dimensions: int) -> None:
        await asyncio.to_thread(self._ensure_collection_sync, dimensions)

    def _ensure_collection_sync(self, dimensions: int) -> None:
        if dimensions <= 0:
            raise VectorStoreError("向量维度必须为正数")
        if self._client.has_collection(self._collection):
            existing = self._existing_dimensions()
            if existing and existing != dimensions:
                raise VectorStoreError(
                    f"集合 {self._collection} 维度为 {existing}，与当前嵌入供给 {dimensions} 不一致"
                )
            self._dimensions = existing or dimensions
            return
        schema = self._client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field(
            "chunk_id", DataType.VARCHAR, max_length=PRIMARY_MAX_LENGTH, is_primary=True
        )
        schema.add_field("kb_id", DataType.VARCHAR, max_length=KEY_MAX_LENGTH)
        schema.add_field("document_id", DataType.VARCHAR, max_length=KEY_MAX_LENGTH)
        schema.add_field("ordinal", DataType.INT64)
        schema.add_field(
            TEXT_FIELD,
            DataType.VARCHAR,
            max_length=TEXT_MAX_LENGTH,
            enable_analyzer=True,
            analyzer_params={"type": "chinese"},
        )
        schema.add_field(SPARSE_FIELD, DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field(DEFAULT_VECTOR_FIELD, DataType.FLOAT_VECTOR, dim=dimensions)
        schema.add_function(
            Function(
                name="text_bm25",
                input_field_names=[TEXT_FIELD],
                output_field_names=[SPARSE_FIELD],
                function_type=FunctionType.BM25,
            )
        )
        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name=SPARSE_FIELD, index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
        )
        index_params.add_index(
            field_name=DEFAULT_VECTOR_FIELD, index_type="AUTOINDEX", metric_type="COSINE"
        )
        self._client.create_collection(self._collection, schema=schema, index_params=index_params)
        self._dimensions = dimensions
        logger.info("milvus collection created: %s dim=%d", self._collection, dimensions)

    def _existing_dimensions(self) -> int:
        try:
            description = self._client.describe_collection(self._collection)
        except Exception as exc:
            raise VectorStoreError(f"读取集合 schema 失败：{exc}") from exc
        for field in description.get("fields", []):
            if field.get("name") == DEFAULT_VECTOR_FIELD:
                params = field.get("params") or {}
                return int(params.get("dim") or 0)
        return 0

    async def upsert(self, vectors: Sequence[ChunkVector]) -> int:
        if not vectors:
            return 0
        dims = len(vectors[0].dense)
        for vector in vectors:
            if len(vector.dense) != dims:
                raise VectorStoreError("同一批次向量维度不一致")
        await self.ensure_collection(dimensions=dims)
        rows = [
            {
                "chunk_id": vector.chunk_id,
                "kb_id": vector.kb_id,
                "document_id": vector.document_id,
                "ordinal": vector.ordinal,
                TEXT_FIELD: vector.text[:TEXT_MAX_LENGTH],
                DEFAULT_VECTOR_FIELD: vector.dense,
            }
            for vector in vectors
        ]
        await asyncio.to_thread(self._upsert_sync, rows)
        return len(rows)

    def _upsert_sync(self, rows: list[dict[str, Any]]) -> None:
        try:
            self._client.upsert(self._collection, rows)
            self._client.flush(self._collection)
        except Exception as exc:
            raise VectorStoreError(f"向量写入失败：{exc}") from exc

    async def delete_document(self, document_id: str) -> None:
        if not self._client.has_collection(self._collection):
            return
        await asyncio.to_thread(self._delete_sync, document_id)

    def _delete_sync(self, document_id: str) -> None:
        try:
            self._client.delete(self._collection, filter=f'document_id == "{document_id}"')
            self._client.flush(self._collection)
        except Exception as exc:
            raise VectorStoreError(f"向量删除失败：{exc}") from exc

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_vector: list[float],
        limit: int,
        kb_ids: Sequence[str] | None = None,
        document_ids: Sequence[str] | None = None,
    ) -> list[tuple[str, float]]:
        """宽捞：稠密向量与 BM25 稀疏各取前 N，RRF 融合后返回 (chunk_id, score)。"""
        if not self._client.has_collection(self._collection):
            return []
        filter_expr = _build_filter(kb_ids=kb_ids, document_ids=document_ids)
        sparse_request = AnnSearchRequest(
            data=[query_text],
            anns_field=SPARSE_FIELD,
            param={"metric_type": "BM25"},
            limit=limit,
            expr=filter_expr,
        )
        dense_request = AnnSearchRequest(
            data=[query_vector],
            anns_field=DEFAULT_VECTOR_FIELD,
            param={"metric_type": "COSINE"},
            limit=limit,
            expr=filter_expr,
        )
        return await asyncio.to_thread(
            self._hybrid_search_sync, sparse_request, dense_request, limit
        )

    def _hybrid_search_sync(
        self, sparse_request: AnnSearchRequest, dense_request: AnnSearchRequest, limit: int
    ) -> list[tuple[str, float]]:
        try:
            results = self._client.hybrid_search(
                self._collection,
                reqs=[sparse_request, dense_request],
                ranker=RRFRanker(60),
                limit=limit,
                output_fields=["chunk_id"],
            )
        except Exception as exc:
            raise VectorStoreError(f"混合检索失败：{exc}") from exc
        hits: list[tuple[str, float]] = []
        for page in results:
            for hit in page:
                entity = hit.get("entity") or {}
                chunk_id = entity.get("chunk_id") or hit.get("id")
                if chunk_id is None:
                    continue
                hits.append((str(chunk_id), float(hit.get("distance") or 0.0)))
        return hits


def _build_filter(
    *, kb_ids: Sequence[str] | None = None, document_ids: Sequence[str] | None = None
) -> str | None:
    clauses: list[str] = []
    if kb_ids:
        clauses.append(f"kb_id in [{_quote_list(kb_ids)}]")
    if document_ids:
        clauses.append(f"document_id in [{_quote_list(document_ids)}]")
    if not clauses:
        return None
    return " and ".join(clauses)


def _quote_list(values: Sequence[str]) -> str:
    return ", ".join(f'"{value}"' for value in values)
