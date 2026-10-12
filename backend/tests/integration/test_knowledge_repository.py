"""知识库仓储集成测试（真实 PostgreSQL）：schema 约束、条件抢占与终态守卫。

覆盖 P3 机制基线 §2.8：parsing/indexing 并发不双写、claim 过期恢复、重复索引不产生双份。
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.knowledge import KbChunk, KbDocument
from app.repositories.knowledge_repository import (
    KbChunkRepository,
    KbDocumentRepository,
    KnowledgeBaseRepository,
)
from tests.integration.db_utils import (
    inspect_tables,
    run_with_connection,
    run_with_session,
    run_with_session_factory,
)

pytestmark = pytest.mark.integration

KB_TABLES = {"knowledge_bases", "kb_documents", "kb_chunks"}


def test_knowledge_tables_created(runtime_db_url: str) -> None:
    tables = run_with_connection(runtime_db_url, inspect_tables)
    assert KB_TABLES <= set(tables)

    documents = tables["kb_documents"]
    assert "uq_kb_documents_content" in documents["unique_constraints"]
    assert "ix_kb_documents_kb_status" in documents["indexes"]
    claim_index = documents["indexes"]["ix_kb_documents_claim_expires"]
    assert "parsing" in str(claim_index["dialect_options"]["postgresql_where"]), (
        "claim 恢复查询依赖在途状态的部分索引"
    )

    chunks = tables["kb_chunks"]
    assert "uq_kb_chunks_ordinal" in chunks["unique_constraints"]


async def _seed_document(
    session: AsyncSession, *, status: str = "uploaded", checksum: str | None = None
) -> KbDocument:
    kb = await KnowledgeBaseRepository(session).create(
        code=f"kb-{uuid4().hex[:8]}", name="酒店知识库"
    )
    document = await KbDocumentRepository(session).create(
        kb_id=kb.id,
        title="入住政策.md",
        source_type="markdown",
        object_key=f"kb/{uuid4().hex}.md",
        size_bytes=1024,
        checksum=checksum or uuid4().hex,
    )
    document.status = status
    await session.flush()
    return document


def test_document_checksum_unique_per_kb(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> bool:
        first = await _seed_document(session, checksum="same-content")
        await session.commit()

        conflicted = False
        try:
            await KbDocumentRepository(session).create(
                kb_id=first.kb_id,
                title="重复上传.md",
                source_type="markdown",
                object_key=f"kb/{uuid4().hex}.md",
                size_bytes=1024,
                checksum="same-content",
            )
            await session.commit()
        except IntegrityError:
            conflicted = True
            await session.rollback()
        return conflicted

    assert run_with_session(runtime_db_url, _body) is True


def test_claim_from_candidate_and_owner_guard(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        document = await _seed_document(session)
        await session.commit()

        repo = KbDocumentRepository(session)
        assert await repo.claim(
            document.id, target="parsing", owner_token="owner-a", lease_seconds=120
        )
        # 在途状态下第二个 worker 抢不到（不双写）
        assert not await repo.claim(
            document.id, target="parsing", owner_token="owner-b", lease_seconds=120
        )
        # 反向抢占同样不可能：parsing 不是 indexing 的候选状态
        assert not await repo.claim(
            document.id, target="indexing", owner_token="owner-b", lease_seconds=120
        )
        # 非持有者不能终结
        assert not await repo.mark_parsed(document.id, owner_token="owner-b")
        assert await repo.mark_parsed(
            document.id, owner_token="owner-a", parse_object_key="kb/x.txt"
        )
        await session.commit()

        refreshed = await repo.get_by_id(document.id)
        assert refreshed is not None
        return {
            "status": refreshed.status,
            "claim_owner": refreshed.claim_owner,
            "claim_expires_at": refreshed.claim_expires_at,
            "attempt_count": refreshed.attempt_count,
            "parsed_at": refreshed.parsed_at,
            "parse_object_key": refreshed.parse_object_key,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["status"] == "parsed"
    assert values["claim_owner"] is None
    assert values["claim_expires_at"] is None
    assert values["attempt_count"] == 1
    assert values["parsed_at"] is not None
    assert values["parse_object_key"] == "kb/x.txt"


def test_claim_concurrent_exactly_one_wins(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            document = await _seed_document(session)
            await session.commit()
            document_id = document.id

        async def _try(token: str) -> bool:
            async with factory() as session:
                result = await KbDocumentRepository(session).claim(
                    document_id, target="parsing", owner_token=token, lease_seconds=120
                )
                await session.commit()
                return result

        results = await asyncio.gather(_try("owner-a"), _try("owner-b"))
        async with factory() as session:
            refreshed = await KbDocumentRepository(session).get_by_id(document_id)
            assert refreshed is not None
            return {"wins": sorted(results), "owner": refreshed.claim_owner}

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["wins"] == [False, True]
    assert values["owner"] in {"owner-a", "owner-b"}


def test_failed_then_retry_chain(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> list[str]:
        document = await _seed_document(session)
        await session.commit()
        repo = KbDocumentRepository(session)
        statuses: list[str] = []

        async def _status() -> str:
            refreshed = await repo.get_by_id(document.id)
            assert refreshed is not None
            return refreshed.status

        assert await repo.claim(document.id, target="parsing", owner_token="w1", lease_seconds=60)
        assert await repo.mark_failed(
            document.id, owner_token="w1", target="error_parsing", error="pdf 解析失败"
        )
        await session.commit()
        statuses.append(await _status())

        # error_parsing 是 parsing 的候选状态：可再次抢占重试
        assert await repo.claim(document.id, target="parsing", owner_token="w2", lease_seconds=60)
        assert await repo.mark_parsed(document.id, owner_token="w2")
        await session.commit()
        statuses.append(await _status())

        assert await repo.claim(document.id, target="indexing", owner_token="w2", lease_seconds=60)
        assert await repo.mark_indexed(document.id, owner_token="w2", chunk_count=3)
        await session.commit()
        statuses.append(await _status())

        refreshed = await repo.get_by_id(document.id)
        assert refreshed is not None
        statuses.append(str(refreshed.chunk_count))
        return statuses

    assert run_with_session(runtime_db_url, _body) == ["error_parsing", "parsed", "indexed", "3"]


def test_release_expired_claims(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        document = await _seed_document(session, status="parsed")
        await session.commit()
        repo = KbDocumentRepository(session)
        assert await repo.claim(
            document.id, target="indexing", owner_token="dead", lease_seconds=-60
        )
        await session.commit()

        released = await repo.release_expired_claims()
        await session.commit()

        refreshed = await repo.get_by_id(document.id)
        assert refreshed is not None
        return {
            "released": released,
            "status": refreshed.status,
            "error": refreshed.error,
            "claim_owner": refreshed.claim_owner,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["released"] == 1
    assert values["status"] == "error_indexing"
    assert values["error"] == "claim_expired"
    assert values["claim_owner"] is None


def test_release_expired_claims_ignores_live_claims(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> int:
        document = await _seed_document(session)
        await session.commit()
        repo = KbDocumentRepository(session)
        assert await repo.claim(
            document.id, target="parsing", owner_token="alive", lease_seconds=600
        )
        await session.commit()
        return await repo.release_expired_claims()

    assert run_with_session(runtime_db_url, _body) == 0


def test_replace_for_document_is_idempotent(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        document = await _seed_document(session)
        await session.commit()
        chunks = KbChunkRepository(session)
        payload = [
            (0, "入住时间 14:00", "h-0", 0, 10),
            (1, "退房时间 12:00", "h-1", 11, 21),
        ]
        assert (
            await chunks.replace_for_document(
                kb_id=document.kb_id, document_id=document.id, chunks=payload
            )
            == 2
        )
        await session.commit()

        # 重建索引：整篇替换不产生双份
        assert (
            await chunks.replace_for_document(
                kb_id=document.kb_id,
                document_id=document.id,
                chunks=[(0, "WiFi 密码见房卡", "h-0", 0, 12)],
            )
            == 1
        )
        await session.commit()

        rows = await chunks.list_by_document(document.id)
        status = await _document_status(session, document.id)
        return {
            "count": await chunks.count_by_document(document.id),
            "contents": [row.content for row in rows],
            "ordinals": [row.ordinal for row in rows],
            "status": status,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["count"] == 1
    assert values["contents"] == ["WiFi 密码见房卡"]
    assert values["ordinals"] == [0]
    assert values["status"] == "uploaded"


async def _document_status(session: AsyncSession, document_id: object) -> str | None:
    document = await KbDocumentRepository(session).get_by_id(document_id)
    return document.status if document is not None else None


def test_chunk_ordinal_unique_guard(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> bool:
        document = await _seed_document(session)
        await session.commit()
        session.add_all(
            [
                KbChunk(
                    kb_id=document.kb_id,
                    document_id=document.id,
                    ordinal=0,
                    content="第一块",
                    content_hash="h-0",
                ),
                KbChunk(
                    kb_id=document.kb_id,
                    document_id=document.id,
                    ordinal=0,
                    content="重复序号",
                    content_hash="h-1",
                ),
            ]
        )
        conflicted = False
        try:
            await session.commit()
        except IntegrityError:
            conflicted = True
            await session.rollback()
        return conflicted

    assert run_with_session(runtime_db_url, _body) is True


def test_claim_lease_expiry_is_recorded(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        document = await _seed_document(session)
        await session.commit()
        repo = KbDocumentRepository(session)
        assert await repo.claim(document.id, target="parsing", owner_token="w", lease_seconds=90)
        await session.commit()

        refreshed = await repo.get_by_id(document.id)
        assert refreshed is not None
        row = (
            await session.execute(
                select(KbDocument.updated_at, KbDocument.claim_expires_at).where(
                    KbDocument.id == document.id
                )
            )
        ).one()
        return {
            "expires_after_updated": (row.claim_expires_at - row.updated_at).total_seconds(),
            "status": refreshed.status,
        }

    values = run_with_session(runtime_db_url, _body)
    assert 85 <= values["expires_after_updated"] <= 95
    assert values["status"] == "parsing"


def test_document_not_found_claim_and_finish_are_false(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> list[bool]:
        repo = KbDocumentRepository(session)
        missing = uuid4()
        claimed = await repo.claim(missing, target="parsing", owner_token="w", lease_seconds=60)
        finished = await repo.mark_parsed(missing, owner_token="w")
        return [claimed, finished]

    assert run_with_session(runtime_db_url, _body) == [False, False]


def test_uploaded_document_defaults(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        document = await _seed_document(session)
        await session.commit()

        kb = await KnowledgeBaseRepository(session).get_by_id(document.kb_id)
        refreshed = await KbDocumentRepository(session).get_by_id(document.id)
        assert kb is not None and refreshed is not None
        listable = await KbDocumentRepository(session).list_by_status(["uploaded"])
        by_checksum = await KbDocumentRepository(session).get_by_checksum(
            kb_id=document.kb_id, checksum=document.checksum
        )
        return {
            "scope": kb.scope,
            "active": kb.active,
            "status": refreshed.status,
            "chunk_count": refreshed.chunk_count,
            "attempt_count": refreshed.attempt_count,
            "listed": any(row.id == document.id for row in listable),
            "checksum_hit": by_checksum is not None and by_checksum.id == document.id,
        }

    values = run_with_session(runtime_db_url, _body)
    assert values == {
        "scope": "guest",
        "active": True,
        "status": "uploaded",
        "chunk_count": 0,
        "attempt_count": 0,
        "listed": True,
        "checksum_hit": True,
    }


def test_force_stale_status_write_is_rejected(runtime_db_url: str) -> None:
    """终态写入必须走 owner 守卫路径；陈旧 worker 的盲写无法命中行。"""

    async def _body(session: AsyncSession) -> bool:
        document = await _seed_document(session)
        await session.commit()
        repo = KbDocumentRepository(session)
        assert await repo.claim(document.id, target="parsing", owner_token="w1", lease_seconds=-60)
        await session.commit()
        await repo.release_expired_claims()
        await session.commit()

        # w1 已失联：过期释放后，w1 的 mark_indexed 不再命中
        return await repo.mark_indexed(document.id, owner_token="w1", chunk_count=2)

    assert run_with_session(runtime_db_url, _body) is False


def test_chunk_get_many_returns_text(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> list[str]:
        document = await _seed_document(session)
        await session.commit()
        chunks = KbChunkRepository(session)
        await chunks.replace_for_document(
            kb_id=document.kb_id,
            document_id=document.id,
            chunks=[
                (0, "早餐 07:00-09:30", "h-0", None, None),
                (1, "WiFi：haochen-guest", "h-1", None, None),
            ],
        )
        await session.commit()

        rows = await chunks.list_by_document(document.id)
        fetched = await chunks.get_many([rows[1].id, rows[0].id])
        by_id = {row.id: row.content for row in fetched}
        return [by_id[rows[1].id], by_id[rows[0].id]]

    assert run_with_session(runtime_db_url, _body) == ["WiFi：haochen-guest", "早餐 07:00-09:30"]
