"""知识库仓储：配置读写、文档状态机与条件抢占、分块正文替换。

不变量：
- 文档读取一律 `populate_existing`，避免 identity map 回放旧状态（P2 教训）；
- parsing / indexing 为条件抢占：只允许从候选状态集合 UPDATE 成功一次，在途行不会被
  第二个 worker 抢走（并发不双写）；claim 过期由恢复路径重置为 error 状态；
- 终态写入（parsed / indexed / error_*）必须持有当前 claim owner token 才生效。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import KB_INDEX_CLAIM_STATUSES, KB_PARSE_CLAIM_STATUSES
from app.models.knowledge import KbChunk, KbDocument, KnowledgeBase
from app.repositories.support import affected_rows

KB_CLAIM_STATUSES: dict[str, frozenset[str]] = {
    "parsing": KB_PARSE_CLAIM_STATUSES,
    "indexing": KB_INDEX_CLAIM_STATUSES,
}
KB_CLAIM_FAILED_STATUS: dict[str, str] = {
    "parsing": "error_parsing",
    "indexing": "error_indexing",
}


class KnowledgeBaseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        code: str,
        name: str,
        description: str | None = None,
        scope: str = "guest",
    ) -> KnowledgeBase:
        kb = KnowledgeBase(code=code, name=name, description=description, scope=scope)
        self._session.add(kb)
        await self._session.flush()
        return kb

    async def get_by_id(self, kb_id: object) -> KnowledgeBase | None:
        return await self._session.get(KnowledgeBase, kb_id, populate_existing=True)

    async def get_by_code(self, code: str) -> KnowledgeBase | None:
        stmt = (
            select(KnowledgeBase)
            .where(KnowledgeBase.code == code)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(self, *, active_only: bool = False) -> list[KnowledgeBase]:
        stmt = select(KnowledgeBase).order_by(KnowledgeBase.code)
        if active_only:
            stmt = stmt.where(KnowledgeBase.active.is_(True))
        stmt = stmt.execution_options(populate_existing=True)
        return list((await self._session.execute(stmt)).scalars())


class KbDocumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        kb_id: object,
        title: str,
        source_type: str,
        object_key: str,
        size_bytes: int,
        checksum: str,
        uploaded_by: object | None = None,
    ) -> KbDocument:
        document = KbDocument(
            kb_id=kb_id,
            title=title,
            source_type=source_type,
            object_key=object_key,
            size_bytes=size_bytes,
            checksum=checksum,
            uploaded_by=uploaded_by,
        )
        self._session.add(document)
        await self._session.flush()
        return document

    async def get_by_id(self, document_id: object) -> KbDocument | None:
        return await self._session.get(KbDocument, document_id, populate_existing=True)

    async def get_by_checksum(self, *, kb_id: object, checksum: str) -> KbDocument | None:
        stmt = (
            select(KbDocument)
            .where(KbDocument.kb_id == kb_id, KbDocument.checksum == checksum)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_many(self, document_ids: Sequence[object]) -> list[KbDocument]:
        """批量按主键读取：检索回填标题与状态用，避免逐块往返。"""
        if not document_ids:
            return []
        stmt = (
            select(KbDocument)
            .where(KbDocument.id.in_(tuple(set(document_ids))))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_by_kb(
        self, kb_id: object, *, status: str | None = None, limit: int = 200
    ) -> list[KbDocument]:
        stmt = select(KbDocument).where(KbDocument.kb_id == kb_id)
        if status is not None:
            stmt = stmt.where(KbDocument.status == status)
        stmt = (
            stmt.order_by(KbDocument.created_at.desc(), KbDocument.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_by_status(
        self, statuses: Iterable[str], *, limit: int = 100
    ) -> list[KbDocument]:
        stmt = (
            select(KbDocument)
            .where(KbDocument.status.in_(tuple(statuses)))
            .order_by(KbDocument.created_at, KbDocument.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def claim(
        self,
        document_id: object,
        *,
        target: str,
        owner_token: str,
        lease_seconds: int,
    ) -> bool:
        """条件抢占 parsing / indexing：只从候选状态集合迁移一次，返回是否抢到。"""
        candidates = KB_CLAIM_STATUSES[target]
        now = func.now()
        stmt = (
            update(KbDocument)
            .where(KbDocument.id == document_id, KbDocument.status.in_(tuple(candidates)))
            .values(
                status=target,
                claim_owner=owner_token,
                claim_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=KbDocument.attempt_count + 1,
                error=None,
                updated_at=now,
            )
        )
        return affected_rows(await self._session.execute(stmt)) == 1

    async def mark_parsed(
        self,
        document_id: object,
        *,
        owner_token: str,
        parse_object_key: str | None = None,
    ) -> bool:
        return await self._finish(
            document_id,
            owner_token=owner_token,
            values={"status": "parsed", "parsed_at": func.now(), "error": None},
            extra={"parse_object_key": parse_object_key} if parse_object_key else {},
        )

    async def mark_indexed(
        self, document_id: object, *, owner_token: str, chunk_count: int
    ) -> bool:
        return await self._finish(
            document_id,
            owner_token=owner_token,
            values={
                "status": "indexed",
                "indexed_at": func.now(),
                "chunk_count": chunk_count,
                "error": None,
            },
        )

    async def mark_failed(
        self, document_id: object, *, owner_token: str, target: str, error: str
    ) -> bool:
        return await self._finish(
            document_id,
            owner_token=owner_token,
            values={"status": target, "error": error[:2000]},
        )

    async def release_expired_claims(self, *, target: str | None = None) -> int:
        """恢复路径：claim 过期的在途行置 error 状态，等待下一次条件抢占重试。"""
        targets = (target,) if target is not None else tuple(KB_CLAIM_STATUSES)
        released = 0
        now = func.now()
        for current in targets:
            stmt = (
                update(KbDocument)
                .where(
                    KbDocument.status == current,
                    KbDocument.claim_expires_at.is_not(None),
                    KbDocument.claim_expires_at < now,
                )
                .values(
                    status=KB_CLAIM_FAILED_STATUS[current],
                    error="claim_expired",
                    claim_owner=None,
                    claim_expires_at=None,
                    updated_at=now,
                )
            )
            released += affected_rows(await self._session.execute(stmt))
        return released

    async def _finish(
        self,
        document_id: object,
        *,
        owner_token: str,
        values: dict[str, object],
        extra: dict[str, object] | None = None,
    ) -> bool:
        """终态写入守卫：只接受当前 claim owner，写入即释放 claim。"""
        payload: dict[str, object] = dict(values)
        payload.update(extra or {})
        payload.update({"claim_owner": None, "claim_expires_at": None, "updated_at": func.now()})
        stmt = (
            update(KbDocument)
            .where(KbDocument.id == document_id, KbDocument.claim_owner == owner_token)
            .values(**payload)
        )
        return affected_rows(await self._session.execute(stmt)) == 1


class KbChunkRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def replace_for_document(
        self,
        *,
        kb_id: object,
        document_id: object,
        chunks: Sequence[tuple[int, str, str, int | None, int | None]],
    ) -> int:
        """整篇替换：先删后插，与状态终态在同一事务提交（重复索引不产生双份）。"""
        await self._session.execute(delete(KbChunk).where(KbChunk.document_id == document_id))
        for ordinal, content, content_hash, char_start, char_end in chunks:
            self._session.add(
                KbChunk(
                    kb_id=kb_id,
                    document_id=document_id,
                    ordinal=ordinal,
                    content=content,
                    content_hash=content_hash,
                    char_start=char_start,
                    char_end=char_end,
                )
            )
        await self._session.flush()
        return len(chunks)

    async def list_by_document(self, document_id: object) -> list[KbChunk]:
        stmt = (
            select(KbChunk)
            .where(KbChunk.document_id == document_id)
            .order_by(KbChunk.ordinal)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get_many(self, chunk_ids: Sequence[object]) -> list[KbChunk]:
        if not chunk_ids:
            return []
        stmt = (
            select(KbChunk)
            .where(KbChunk.id.in_(tuple(chunk_ids)))
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def count_by_document(self, document_id: object) -> int:
        stmt = select(func.count()).select_from(KbChunk).where(KbChunk.document_id == document_id)
        return int((await self._session.execute(stmt)).scalar_one())
