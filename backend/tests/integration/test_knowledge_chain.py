"""知识库上传链路集成测试（真实 PostgreSQL）：解析 → 分块 → 索引 → 补偿 → 种子幂等。

外部依赖（MinIO / Milvus / 嵌入接口）以进程内替身注入：链路的事实边界是 PG 状态与
kb_chunks，替身只负责证明「写到哪些键、写了哪些向量」。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.knowledge import seed as knowledge_seed
from app.knowledge import task_handlers
from app.knowledge.embedding import DeterministicEmbeddings
from app.knowledge.parsing import extract_text
from app.knowledge.recovery import release_expired_claim_documents
from app.knowledge.upload_service import (
    DocumentDuplicateError,
    KnowledgeBaseMissingError,
    KnowledgeUploadService,
    UnsupportedSourceTypeError,
)
from app.repositories import KbChunkRepository, KbDocumentRepository, KnowledgeBaseRepository
from app.services.task_service import TaskExecutor, submit_task
from tests.integration.db_utils import run_with_session, run_with_session_factory
from tests.integration.knowledge_fakes import (
    FakeObjectStorage,
    FakeVectorStore,
    RecordingTaskDispatcher,
    fake_upload_service,
)

pytestmark = pytest.mark.integration

SAMPLE_TEXT = (
    "# 早餐说明\n\n"
    "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅，凭房号入座，无需餐券。\n\n"
    "房价不含早餐，单独购买 38 元/位，1.2 米以下儿童 19 元/位。\n\n"
    "最晚可以在当天 21:00 之前在小程序内加购，次日生效。\n\n"
    "21:30 之后不再提供厨房出品，1 楼自动售货机 24 小时开放。"
)


@pytest.fixture(autouse=True)
def _clean_fakes() -> None:
    FakeObjectStorage.reset()
    FakeVectorStore.reset()
    yield
    FakeObjectStorage.reset()
    FakeVectorStore.reset()


@pytest.fixture(autouse=True)
def _patch_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_handlers, "ObjectStorage", FakeObjectStorage)
    monkeypatch.setattr(task_handlers, "ChunkVectorStore", FakeVectorStore)
    monkeypatch.setattr(task_handlers, "build_embedding_provider", DeterministicEmbeddings)
    monkeypatch.setattr(knowledge_seed, "KnowledgeUploadService", fake_upload_service)
    # 缩小分块阈值，让样例正文必然切成多块（线上阈值由配置决定）
    monkeypatch.setattr(get_settings(), "kb_chunk_max_chars", 120)


def test_upload_parse_index_chain_persists_chunks_and_vectors(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        dispatcher = RecordingTaskDispatcher()
        executor = TaskExecutor(dispatcher=dispatcher, session_factory=factory)
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"chain-{uuid4().hex[:8]}", name="链路测试库", scope="guest"
            )
            await session.commit()
            outcome = await KnowledgeUploadService(session, storage=FakeObjectStorage()).upload(
                kb_id=kb.id,
                title="早餐说明",
                source_type="markdown",
                filename="breakfast.md",
                data=SAMPLE_TEXT.encode(),
            )
            document_id = outcome.document.id
            parse_task_id = outcome.submission.task_id
            assert outcome.document.status == "uploaded"

        parse_outcome = await executor.execute(parse_task_id)
        async with factory() as session:
            document = await KbDocumentRepository(session).get_by_id(document_id)
            chunks = await KbChunkRepository(session).list_by_document(document_id)
            assert document is not None
            parsed_status = document.status
            parse_artifact = document.parse_object_key
        assert parse_outcome == "succeeded"
        assert parsed_status == "parsed"
        assert parse_artifact is not None
        assert FakeObjectStorage.objects[parse_artifact].decode() == extract_text(
            source_type="markdown", data=SAMPLE_TEXT.encode()
        )
        assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
        assert all(chunk.content in SAMPLE_TEXT for chunk in chunks)
        assert all(chunk.content_hash for chunk in chunks)

        assert len(dispatcher.enqueued) == 1
        index_task_id = dispatcher.enqueued[0]
        assert await executor.execute(index_task_id) == "succeeded"

        async with factory() as session:
            document = await KbDocumentRepository(session).get_by_id(document_id)
            assert document is not None
            indexed_status = document.status
            chunk_count = document.chunk_count
        vectors = FakeVectorStore().documents(document_id)
        assert indexed_status == "indexed"
        assert chunk_count == len(chunks) == len(vectors)
        assert {vector.chunk_id for vector in vectors} == {str(chunk.id) for chunk in chunks}
        assert {len(vector.dense) for vector in vectors} == {64}
        assert all(vector.text in SAMPLE_TEXT for vector in vectors)

        # 重复投递索引任务：终态不可再抢占，不产生第二次写入
        assert await executor.execute(index_task_id) == "not_claimable"
        assert len(FakeVectorStore().documents(document_id)) == len(vectors)
        return {"chunks": len(chunks), "enqueued": len(dispatcher.enqueued)}

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["enqueued"] == 1
    assert values["chunks"] > 1


def test_parse_failure_marks_document_error_parsing(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        executor = TaskExecutor(dispatcher=RecordingTaskDispatcher(), session_factory=factory)
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"parse-{uuid4().hex[:8]}", name="解析失败库"
            )
            await session.commit()
            outcome = await KnowledgeUploadService(session, storage=FakeObjectStorage()).upload(
                kb_id=kb.id,
                title="空文档",
                source_type="markdown",
                filename="empty.md",
                data=b"   \n\n   ",
            )
            document_id = outcome.document.id

        assert await executor.execute(outcome.submission.task_id) == "failed"
        async with factory() as session:
            document = await KbDocumentRepository(session).get_by_id(document_id)
            assert document is not None
            return {
                "status": document.status,
                "error": document.error,
                "owner": document.claim_owner,
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["status"] == "error_parsing"
    assert "解析结果为空" in str(values["error"])
    assert values["owner"] is None


def test_index_failure_compensates_to_error_indexing_and_is_retryable(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        dispatcher = RecordingTaskDispatcher()
        executor = TaskExecutor(dispatcher=dispatcher, session_factory=factory)
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"index-{uuid4().hex[:8]}", name="索引失败库"
            )
            await session.commit()
            outcome = await KnowledgeUploadService(session, storage=FakeObjectStorage()).upload(
                kb_id=kb.id,
                title="早餐说明",
                source_type="markdown",
                filename="breakfast.md",
                data=SAMPLE_TEXT.encode(),
            )
            document_id = outcome.document.id

        assert await executor.execute(outcome.submission.task_id) == "succeeded"
        index_task_id = dispatcher.enqueued[0]
        FakeVectorStore.fail_upsert = True
        assert await executor.execute(index_task_id) == "failed"
        FakeVectorStore.fail_upsert = False

        async with factory() as session:
            document = await KbDocumentRepository(session).get_by_id(document_id)
            assert document is not None
            failed_status = document.status
            error = document.error
            chunk_count = document.chunk_count
            chunks = await KbChunkRepository(session).list_by_document(document_id)
        assert failed_status == "error_indexing"
        assert "向量写入失败" in str(error)
        assert FakeVectorStore().documents(document_id) == []
        assert chunk_count == 0

        # 重试：error_indexing 仍是索引候选状态，重新提交索引任务后块数补齐
        retry = await submit_task(
            task_type=task_handlers.INDEX_TASK_TYPE,
            handler_version=task_handlers.HANDLER_VERSION,
            payload={"document_id": str(document_id)},
            dedupe_key=task_handlers.index_dedupe_key(document_id),
            dispatcher=dispatcher,
            session_factory=factory,
        )
        assert await executor.execute(retry.task_id) == "succeeded"

        async with factory() as session:
            document = await KbDocumentRepository(session).get_by_id(document_id)
            assert document is not None
            retried_status, retried_count = document.status, document.chunk_count
        retried = FakeVectorStore().documents(document_id)
        return {
            "failed_status": failed_status,
            "retried_status": retried_status,
            "chunks": len(chunks),
            "retried_count": retried_count,
            "retried_vectors": len(retried),
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["failed_status"] == "error_indexing"
    assert values["retried_status"] == "indexed"
    assert values["chunks"] == values["retried_vectors"] == values["retried_count"]
    assert values["chunks"] > 1


def test_upload_rejects_duplicate_and_unknown_knowledge_base(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, bool]:
        kb = await KnowledgeBaseRepository(session).create(
            code=f"dup-{uuid4().hex[:8]}", name="重复内容库"
        )
        await session.commit()
        service = KnowledgeUploadService(session, storage=FakeObjectStorage())
        await service.upload(
            kb_id=kb.id,
            title="早餐说明",
            source_type="markdown",
            filename="breakfast.md",
            data=SAMPLE_TEXT.encode(),
        )
        duplicate, missing, unsupported = False, False, False
        try:
            await service.upload(
                kb_id=kb.id,
                title="早餐说明副本",
                source_type="markdown",
                filename="breakfast.md",
                data=SAMPLE_TEXT.encode(),
            )
        except DocumentDuplicateError:
            duplicate = True
        try:
            await service.upload(
                kb_id=uuid4(),
                title="未知库",
                source_type="markdown",
                filename="x.md",
                data=b"# x",
            )
        except KnowledgeBaseMissingError:
            missing = True
        try:
            await service.upload(
                kb_id=kb.id,
                title="图片",
                source_type="image",
                filename="x.png",
                data=b"binary",
            )
        except UnsupportedSourceTypeError:
            unsupported = True
        await session.rollback()
        return {"duplicate": duplicate, "missing": missing, "unsupported": unsupported}

    values = run_with_session(runtime_db_url, _body)
    assert values == {"duplicate": True, "missing": True, "unsupported": True}


def test_knowledge_seed_is_idempotent_and_claim_release_job_runs(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            first = await knowledge_seed.seed_knowledge(session)
            second = await knowledge_seed.seed_knowledge(session)
            kb = await KnowledgeBaseRepository(session).get_by_code(knowledge_seed.KB_CODE)
            assert kb is not None
            documents = await KbDocumentRepository(session).list_by_kb(kb.id)
            statuses = sorted({document.status for document in documents})
        released = await release_expired_claim_documents(session_factory=factory)
        return {
            "first_uploaded": len(first.uploaded),
            "second_uploaded": len(second.uploaded),
            "second_skipped": second.skipped,
            "kb_created_twice": first.kb_created and second.kb_created,
            "documents": len(documents),
            "statuses": statuses,
            "released": released,
            "titles": sorted(first.uploaded),
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["first_uploaded"] == len(knowledge_seed.SAMPLE_TITLES)
    assert values["second_uploaded"] == 0
    assert values["second_skipped"] == len(knowledge_seed.SAMPLE_TITLES)
    assert values["kb_created_twice"] is False
    assert values["documents"] == len(knowledge_seed.SAMPLE_TITLES)
    assert values["statuses"] == ["uploaded"]
    assert values["released"] == 0
    assert all(title.startswith("【样例】") for title in values["titles"])
