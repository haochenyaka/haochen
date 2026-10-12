"""两段式检索：召回宽捞 → 重排精排 → 上下文组装，回答可溯源到分块。

不变量：
- 第一段在 Milvus 完成稠密 + BM25 的 RRF 宽捞，第二段只在候选内重排并截断；
- 交付模型的上下文只来自 PostgreSQL 的 kb_chunks（Milvus 只提供命中键），
  因此引用里的正文、标题与偏移可与库内事实逐字对齐；
- 重排故障降级为召回顺序并在结果上标记 degraded，检索本身不因重排不可用而失败；
- 只引用 `indexed` 状态的文档，解析中/失败/已停用的文档不会进入上下文。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.knowledge.embedding import EmbeddingProvider, build_embedding_provider
from app.knowledge.milvus_store import ChunkVectorStore
from app.knowledge.rerank import RerankCandidate, Reranker, RerankError, build_reranker
from app.models.knowledge import KbChunk, KbDocument
from app.repositories.knowledge_repository import KbChunkRepository, KbDocumentRepository

logger = logging.getLogger(__name__)

INDEXED_STATUS = "indexed"
SNIPPET_CHARS = 160


class RetrievalError(Exception):
    """检索入参或数据不可用。"""


@dataclass(frozen=True)
class RetrievedChunk:
    """一个进入上下文的检索片段：正文溯源到 kb_chunks，引用序号从 1 开始。"""

    index: int
    chunk_id: UUID
    kb_id: UUID
    document_id: UUID
    document_title: str
    ordinal: int
    content: str
    score: float
    recall_score: float
    char_start: int | None
    char_end: int | None

    def citation(self) -> dict[str, object]:
        return {
            "index": self.index,
            "document_id": str(self.document_id),
            "document_title": self.document_title,
            "chunk_id": str(self.chunk_id),
            "ordinal": self.ordinal,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "score": self.score,
            "snippet": self.content[:SNIPPET_CHARS],
        }


@dataclass(frozen=True)
class RetrievedContext:
    query: str
    chunks: tuple[RetrievedChunk, ...]
    prompt_text: str
    degraded: bool = False
    recalled: int = 0
    dropped: int = 0
    notes: tuple[str, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.chunks

    def citations(self) -> list[dict[str, object]]:
        return [item.citation() for item in self.chunks]


@dataclass(frozen=True)
class _ChunkRow:
    chunk: KbChunk
    document_title: str


class KnowledgeRetriever:
    def __init__(
        self,
        session: AsyncSession,
        *,
        provider: EmbeddingProvider | None = None,
        vector_store: ChunkVectorStore | None = None,
        reranker: Reranker | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._provider = provider or build_embedding_provider()
        self._vector_store = vector_store or ChunkVectorStore.create(
            identity=self._provider.identity, dimensions=self._provider.dimensions
        )
        self._reranker = reranker or build_reranker()

    async def retrieve(
        self,
        query: str,
        *,
        kb_ids: Sequence[UUID] | None = None,
        document_ids: Sequence[UUID] | None = None,
        recall_limit: int | None = None,
        top_n: int | None = None,
        max_chars: int | None = None,
    ) -> RetrievedContext:
        question = query.strip()
        if not question:
            raise RetrievalError("检索问题不能为空")

        recall_limit = recall_limit or self._settings.kb_recall_limit
        top_n = top_n or self._settings.kb_rerank_top_n
        max_chars = max_chars or self._settings.kb_context_max_chars
        if min(recall_limit, top_n, max_chars) <= 0:
            raise RetrievalError("检索参数必须为正数")

        vectors = await self._provider.embed([question])
        if not vectors:
            raise RetrievalError("嵌入供给未返回问题向量")
        hits = await self._vector_store.hybrid_search(
            query_text=question,
            query_vector=vectors[0],
            limit=recall_limit,
            kb_ids=[str(item) for item in kb_ids] if kb_ids else None,
            document_ids=[str(item) for item in document_ids] if document_ids else None,
        )
        rows = await self._load_indexed_chunks([chunk_id for chunk_id, _ in hits])
        candidates = _candidates(hits, rows)
        if not candidates:
            return RetrievedContext(query=question, chunks=(), prompt_text="", recalled=len(hits))

        degraded = False
        try:
            ranked = await self._reranker.rerank(question, candidates, top_n=top_n)
        except RerankError:
            logger.warning("rerank degraded to recall order: query=%s", question, exc_info=True)
            degraded = True
            ranked = [(item.chunk_id, item.recall_score) for item in candidates[:top_n]]

        chunks = self._assemble(ranked, rows, recall_scores=dict(hits), max_chars=max_chars)
        return RetrievedContext(
            query=question,
            chunks=chunks,
            prompt_text=_render_prompt(chunks),
            degraded=degraded,
            recalled=len(candidates),
            dropped=len(candidates) - len(chunks),
        )

    async def _load_indexed_chunks(self, chunk_ids: Sequence[str]) -> dict[UUID, _ChunkRow]:
        """按命中键回填正文与文档标题；只保留仍处于 indexed 的文档。"""
        parsed = [key for key in (_as_uuid(chunk_id) for chunk_id in chunk_ids) if key]
        if not parsed:
            return {}
        chunks = await KbChunkRepository(self._session).get_many(parsed)
        documents = await KbDocumentRepository(self._session).get_many(
            [chunk.document_id for chunk in chunks]
        )
        titles = {document.id: document.title for document in documents if _is_indexed(document)}
        return {
            chunk.id: _ChunkRow(chunk=chunk, document_title=titles[chunk.document_id])
            for chunk in chunks
            if chunk.document_id in titles
        }

    def _assemble(
        self,
        ranked: Sequence[tuple[str, float]],
        rows: dict[UUID, _ChunkRow],
        *,
        recall_scores: dict[str, float],
        max_chars: int,
    ) -> tuple[RetrievedChunk, ...]:
        per_document = self._settings.kb_context_max_chunks_per_document
        counts: dict[UUID, int] = {}
        assembled: list[RetrievedChunk] = []
        used = 0
        for chunk_id, score in ranked:
            key = _as_uuid(chunk_id)
            if key is None or key not in rows:
                continue
            chunk = rows[key].chunk
            if counts.get(chunk.document_id, 0) >= per_document:
                continue
            if assembled and used + len(chunk.content) > max_chars:
                continue
            counts[chunk.document_id] = counts.get(chunk.document_id, 0) + 1
            used += len(chunk.content)
            assembled.append(
                RetrievedChunk(
                    index=len(assembled) + 1,
                    chunk_id=key,
                    kb_id=chunk.kb_id,
                    document_id=chunk.document_id,
                    document_title=rows[key].document_title,
                    ordinal=chunk.ordinal,
                    content=chunk.content,
                    score=score,
                    recall_score=recall_scores.get(chunk_id, 0.0),
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                )
            )
        return tuple(assembled)


def _candidates(
    hits: Sequence[tuple[str, float]], rows: dict[UUID, _ChunkRow]
) -> list[RerankCandidate]:
    candidates: list[RerankCandidate] = []
    seen: set[UUID] = set()
    for chunk_id, score in hits:
        key = _as_uuid(chunk_id)
        if key is None or key in seen or key not in rows:
            continue
        seen.add(key)
        candidates.append(
            RerankCandidate(
                chunk_id=chunk_id,
                content=rows[key].chunk.content,
                title=rows[key].document_title,
                recall_score=score,
            )
        )
    return candidates


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        logger.warning("milvus returned non-uuid chunk id: %s", value)
        return None


def _is_indexed(document: KbDocument) -> bool:
    return document.status == INDEXED_STATUS


def _render_prompt(chunks: Sequence[RetrievedChunk]) -> str:
    if not chunks:
        return ""
    blocks = [
        f"[{item.index}]《{item.document_title}》第 {item.ordinal + 1} 段\n{item.content}"
        for item in chunks
    ]
    return "\n\n".join(blocks)
