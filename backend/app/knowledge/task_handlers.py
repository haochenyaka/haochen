"""知识库任务处理器：解析（原件 → 正文分块）与索引（分块 → 向量 + BM25 字段）。

不变量：
- 文档级 claim（owner token 取 `kb.parse:{task_id}` / `kb.index:{task_id}`）是双写守卫：
  重复投递抢不到第二次，跨任务并发也不双写；
- 解析产物落 MinIO、正文落 PG、向量落 Milvus；PG 行始终是权威状态，Milvus 写入失败
  先清理残留向量再置 error_indexing，绝不留下「状态已成功、向量是半截」的中间态；
- 解析成功后在**同一事务**内提交后续 kb.index 任务（意图与事实不分离），提交后才投递；
- 失败钩子与任务终态同事务：补偿写入被 claim 守卫拒绝只记录，不掩盖已成立的失败事实。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import suppress
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge.embedding import build_embedding_provider
from app.knowledge.milvus_store import ChunkVector, ChunkVectorStore
from app.knowledge.parsing import chunk_text, extract_text
from app.knowledge.storage import ObjectStorage
from app.repositories import KbChunkRepository, KbDocumentRepository
from app.services.task_dispatch import dispatch_task
from app.services.task_registry import TaskContext, task_handler
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

PARSE_TASK_TYPE = "kb.parse"
INDEX_TASK_TYPE = "kb.index"
HANDLER_VERSION = 1
EMBED_BATCH_SIZE = 32
PARSED_TEXT_CONTENT_TYPE = "text/plain; charset=utf-8"


class KnowledgeTaskError(Exception):
    """本次执行不可继续：文档状态或外部依赖不允许，任务据此失败关闭。"""


def _document_id(ctx: TaskContext) -> UUID | None:
    raw = ctx.payload_value("document_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def _parse_owner(ctx: TaskContext) -> str:
    return f"kb.parse:{ctx.task_id}"


def _index_owner(ctx: TaskContext) -> str:
    return f"kb.index:{ctx.task_id}"


def index_dedupe_key(document_id: object) -> str:
    return f"kb.index:{document_id}"


def parse_dedupe_key(document_id: object) -> str:
    return f"kb.parse:{document_id}"


async def _parse_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
    await _compensate(
        session,
        document_id=_document_id(ctx),
        owner_token=_parse_owner(ctx),
        target="error_parsing",
        error=error,
    )


async def _index_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
    await _compensate(
        session,
        document_id=_document_id(ctx),
        owner_token=_index_owner(ctx),
        target="error_indexing",
        error=error,
    )


async def _compensate(
    session: AsyncSession,
    *,
    document_id: UUID | None,
    owner_token: str,
    target: str,
    error: str,
) -> None:
    if document_id is None:
        return
    written = await KbDocumentRepository(session).mark_failed(
        document_id, owner_token=owner_token, target=target, error=error
    )
    if not written:
        logger.warning(
            "kb document compensation skipped (claim lost or already compensated): "
            "document_id=%s target=%s",
            document_id,
            target,
        )


@task_handler(PARSE_TASK_TYPE, HANDLER_VERSION, on_failure=_parse_failure)
async def parse_document(ctx: TaskContext) -> Mapping[str, Any]:
    document_id = _document_id(ctx)
    if document_id is None:
        raise KnowledgeTaskError("kb.parse 缺少 document_id 载荷")

    settings = get_settings()
    async with ctx.session_factory() as session:
        documents = KbDocumentRepository(session)
        document = await documents.get_by_id(document_id)
        if document is None:
            raise KnowledgeTaskError(f"文档不存在：{document_id}")
        kb_id = document.kb_id
        object_key = document.object_key
        source_type = document.source_type
        claimed = await documents.claim(
            document_id,
            target="parsing",
            owner_token=_parse_owner(ctx),
            lease_seconds=settings.kb_claim_lease_seconds,
        )
        await session.commit()

    if not claimed:
        raise KnowledgeTaskError(f"文档当前状态不可解析（可能已在处理中）：{document_id}")

    storage = ObjectStorage.from_settings()
    data = await storage.get_bytes(object_key)
    text = extract_text(source_type=source_type, data=data)
    drafts = chunk_text(
        text,
        max_chars=settings.kb_chunk_max_chars,
        overlap_chars=settings.kb_chunk_overlap_chars,
    )
    parse_object_key = f"parsed/{document_id}.txt"
    await storage.put_bytes(
        parse_object_key, text.encode("utf-8"), content_type=PARSED_TEXT_CONTENT_TYPE
    )

    async with ctx.session_factory() as session:
        await KbChunkRepository(session).replace_for_document(
            kb_id=kb_id,
            document_id=document_id,
            chunks=[
                (draft.ordinal, draft.content, draft.content_hash, draft.char_start, draft.char_end)
                for draft in drafts
            ],
        )
        finished = await KbDocumentRepository(session).mark_parsed(
            document_id, owner_token=_parse_owner(ctx), parse_object_key=parse_object_key
        )
        if not finished:
            await session.rollback()
            raise KnowledgeTaskError(f"解析结果写入被拒（claim 已失效）：{document_id}")
        submission = await TaskService(session).submit(
            task_type=INDEX_TASK_TYPE,
            handler_version=HANDLER_VERSION,
            payload={"document_id": str(document_id)},
            dedupe_key=index_dedupe_key(document_id),
        )
        await session.commit()

    await dispatch_task(ctx.dispatcher, submission.task_id if submission.should_dispatch else None)
    logger.info("kb document parsed: document_id=%s chunks=%d", document_id, len(drafts))
    return {
        "document_id": str(document_id),
        "chunk_count": len(drafts),
        "parse_object_key": parse_object_key,
    }


@task_handler(INDEX_TASK_TYPE, HANDLER_VERSION, on_failure=_index_failure)
async def index_document(ctx: TaskContext) -> Mapping[str, Any]:
    document_id = _document_id(ctx)
    if document_id is None:
        raise KnowledgeTaskError("kb.index 缺少 document_id 载荷")

    settings = get_settings()
    async with ctx.session_factory() as session:
        documents = KbDocumentRepository(session)
        document = await documents.get_by_id(document_id)
        if document is None:
            raise KnowledgeTaskError(f"文档不存在：{document_id}")
        kb_id = document.kb_id
        chunks = await KbChunkRepository(session).list_by_document(document_id)
        claimed = await documents.claim(
            document_id,
            target="indexing",
            owner_token=_index_owner(ctx),
            lease_seconds=settings.kb_claim_lease_seconds,
        )
        await session.commit()

    if not claimed:
        raise KnowledgeTaskError(f"文档当前状态不可索引（可能已在处理中）：{document_id}")
    if not chunks:
        raise KnowledgeTaskError(f"文档没有可索引的分块：{document_id}")

    provider = build_embedding_provider()
    vectors: list[ChunkVector] = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start : start + EMBED_BATCH_SIZE]
        embeddings = await provider.embed([chunk.content for chunk in batch])
        vectors.extend(
            ChunkVector(
                chunk_id=str(chunk.id),
                kb_id=str(kb_id),
                document_id=str(document_id),
                ordinal=chunk.ordinal,
                text=chunk.content,
                dense=vector,
            )
            for chunk, vector in zip(batch, embeddings, strict=True)
        )

    store = ChunkVectorStore.create(identity=provider.identity)
    try:
        await store.delete_document(str(document_id))
        await store.upsert(vectors)
    except Exception:
        # 失败补偿：清掉可能已写进来的半截向量，收敛由失败钩子置 error_indexing
        with suppress(Exception):
            await store.delete_document(str(document_id))
        raise

    async with ctx.session_factory() as session:
        finished = await KbDocumentRepository(session).mark_indexed(
            document_id, owner_token=_index_owner(ctx), chunk_count=len(vectors)
        )
        if not finished:
            await session.rollback()
            raise KnowledgeTaskError(f"索引结果写入被拒（claim 已失效）：{document_id}")
        await session.commit()

    logger.info(
        "kb document indexed: document_id=%s chunks=%d collection=%s",
        document_id,
        len(vectors),
        store.collection,
    )
    return {
        "document_id": str(document_id),
        "chunk_count": len(vectors),
        "collection": store.collection,
    }
