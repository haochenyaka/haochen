"""两段式检索集成测试（真实 PostgreSQL）：召回 → 重排 → 带引用上下文组装。

外部依赖（MinIO / Milvus / 嵌入接口）走进程内替身；检索的权威事实是 PG 的
kb_chunks 与 kb_documents，因此断言可以逐字比对正文、标题与偏移。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.knowledge import task_handlers
from app.knowledge.embedding import DeterministicEmbeddings
from app.knowledge.graph import GraphInsight, MatchedRoomType, NearbyPlace
from app.knowledge.graph_retrieval import GraphRetrieval, extract_graph_query
from app.knowledge.rerank import KeywordReranker, RerankError
from app.knowledge.retrieval import KnowledgeRetriever, RetrievalError
from app.knowledge.upload_service import KnowledgeUploadService
from app.models.knowledge import KbDocument
from app.repositories import KbDocumentRepository, KnowledgeBaseRepository
from app.services.task_service import TaskExecutor
from tests.integration.db_utils import run_with_session, run_with_session_factory
from tests.integration.knowledge_fakes import (
    FakeObjectStorage,
    FakeVectorStore,
    RecordingTaskDispatcher,
)

pytestmark = pytest.mark.integration

BREAKFAST_TEXT = (
    "# 早餐说明\n\n"
    "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅，凭房号入座，无需餐券。\n\n"
    "房价不含早餐，单独购买 38 元/位，1.2 米以下儿童 19 元/位。"
)

WIFI_TEXT = (
    "# 网络与智能设备\n\n"
    "客房 WiFi 名称为 HaoChen-Guest，密码见房卡套，电视与投影支持投屏。\n\n"
    "智能门锁使用小程序内的一次性开锁按钮，无需记忆密码。"
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
    monkeypatch.setattr(get_settings(), "kb_chunk_max_chars", 120)


class _ExplodingReranker:
    async def rerank(self, query: str, candidates: list[Any], *, top_n: int) -> list[Any]:
        raise RerankError("重排模型调用失败：替身注入故障")


class _StubGraphRetriever:
    """图腿替身：走真实问题抽取，返回固定房型与周边事实。"""

    def __init__(self) -> None:
        self.questions: list[str] = []

    async def retrieve(self, question: str) -> GraphRetrieval | None:
        self.questions.append(question)
        query = extract_graph_query(question)
        if query is None:
            return None
        return GraphRetrieval(
            query=query,
            insight=GraphInsight(
                rooms=(
                    MatchedRoomType(
                        code="FAMILY-01",
                        name="亲子家庭房",
                        capacity=4,
                        bed_type="twin",
                        base_price_fen=49800,
                        amenities=("免费 WiFi", "投影仪", "儿童用品"),
                    ),
                ),
                places=(
                    NearbyPlace(
                        code="coffee",
                        name="晨光咖啡（新区店）",
                        category="餐饮",
                        description="早 8:00 开始营业。",
                        distance_m=350,
                        walk_minutes=5,
                    ),
                ),
            ),
        )


async def _index_document(
    factory: async_sessionmaker[AsyncSession], *, kb_id: Any, title: str, data: bytes
) -> Any:
    async with factory() as session:
        outcome = await KnowledgeUploadService(session, storage=FakeObjectStorage()).upload(
            kb_id=kb_id,
            title=title,
            source_type="markdown",
            filename=f"{title}.md",
            data=data,
        )
        document_id = outcome.document.id
        parse_task_id = outcome.submission.task_id

    dispatcher = RecordingTaskDispatcher()
    executor = TaskExecutor(dispatcher=dispatcher, session_factory=factory)
    assert await executor.execute(parse_task_id) == "succeeded"
    assert await executor.execute(dispatcher.enqueued[-1]) == "succeeded"
    async with factory() as session:
        document = await KbDocumentRepository(session).get_by_id(document_id)
        assert document is not None
        assert document.status == "indexed"
    return document_id


def test_two_stage_retrieval_returns_traceable_context(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"retrieval-{uuid4().hex[:8]}", name="检索测试库"
            )
            await session.commit()
            breakfast_document = await _index_document(
                factory, kb_id=kb.id, title="早餐说明", data=BREAKFAST_TEXT.encode()
            )
            await _index_document(factory, kb_id=kb.id, title="网络说明", data=WIFI_TEXT.encode())

        async with factory() as session:
            retriever = KnowledgeRetriever(
                session,
                provider=DeterministicEmbeddings(),
                vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
                reranker=KeywordReranker(),
            )
            context = await retriever.retrieve("早餐时间是几点", top_n=3, max_chars=400)
            stored = await KbDocumentRepository(session).get_by_id(breakfast_document)
            assert stored is not None
            return {
                "query": context.query,
                "titles": [chunk.document_title for chunk in context.chunks],
                "contents": [chunk.content for chunk in context.chunks],
                "document_title": stored.title,
                "prompt_text": context.prompt_text,
                "citations": context.citations(),
                "degraded": context.degraded,
                "recalled": context.recalled,
                "indexes": [chunk.index for chunk in context.chunks],
                "offsets": [(chunk.char_start, chunk.char_end) for chunk in context.chunks],
                "in_parse_artifact": all(
                    chunk.content in BREAKFAST_TEXT for chunk in context.chunks
                ),
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert not values["titles"] or values["titles"][0] == values["document_title"]
    assert any("7:00-9:30" in content for content in values["contents"])
    assert values["indexes"] == list(range(1, len(values["indexes"]) + 1))
    assert values["prompt_text"].startswith("[1]《早餐说明》第 ")
    assert values["citations"][0]["document_id"]
    assert values["citations"][0]["document_title"] == "早餐说明"
    assert values["citations"][0]["ordinal"] >= 0
    assert values["degraded"] is False
    assert values["recalled"] >= len(values["contents"])
    assert values["in_parse_artifact"] is True
    assert all(
        start is not None and end is not None and start < end for start, end in values["offsets"]
    )


def test_retrieval_skips_documents_that_are_not_indexed(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"fresh-{uuid4().hex[:8]}", name="新鲜度测试库"
            )
            await session.commit()
            document_id = await _index_document(
                factory, kb_id=kb.id, title="早餐说明", data=BREAKFAST_TEXT.encode()
            )

        async with factory() as session:
            retriever = KnowledgeRetriever(
                session,
                provider=DeterministicEmbeddings(),
                vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
                reranker=KeywordReranker(),
            )
            before = await retriever.retrieve("早餐时间是几点")

        async with factory() as session:
            await session.execute(
                update(KbDocument)
                .where(KbDocument.id == document_id)
                .values(status="error_indexing")
            )
            await session.commit()

        async with factory() as session:
            retriever = KnowledgeRetriever(
                session,
                provider=DeterministicEmbeddings(),
                vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
                reranker=KeywordReranker(),
            )
            after = await retriever.retrieve("早餐时间是几点")
        return {
            "before": len(before.chunks),
            "after": len(after.chunks),
            "after_empty": after.is_empty,
            "after_recalled": after.recalled,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["before"] > 0
    assert values["after"] == 0
    assert values["after_empty"] is True
    assert values["after_recalled"] > 0


def test_retrieval_degrades_to_recall_order_when_rerank_fails(runtime_db_url: str) -> None:
    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"degrade-{uuid4().hex[:8]}", name="降级测试库"
            )
            await session.commit()
            await _index_document(
                factory, kb_id=kb.id, title="早餐说明", data=BREAKFAST_TEXT.encode()
            )

        async with factory() as session:
            retriever = KnowledgeRetriever(
                session,
                provider=DeterministicEmbeddings(),
                vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
                reranker=_ExplodingReranker(),
            )
            context = await retriever.retrieve("早餐时间是几点", top_n=2)
        return {
            "degraded": context.degraded,
            "chunks": len(context.chunks),
            "scores": [chunk.score for chunk in context.chunks],
            "recall_scores": [chunk.recall_score for chunk in context.chunks],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["degraded"] is True
    assert values["chunks"] > 0
    assert values["scores"] == values["recall_scores"]


def test_retrieval_rejects_blank_query_and_unknown_knowledge_base(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, bool]:
        retriever = KnowledgeRetriever(
            session,
            provider=DeterministicEmbeddings(),
            vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
            reranker=KeywordReranker(),
        )
        blank = False
        try:
            await retriever.retrieve("   ")
        except RetrievalError:
            blank = True
        unknown = await retriever.retrieve("早餐时间是几点", kb_ids=[uuid4()])
        return {"blank": blank, "unknown_empty": unknown.is_empty, "recalled": unknown.recalled}

    values = run_with_session(runtime_db_url, _body)
    assert values == {"blank": True, "unknown_empty": True, "recalled": 0}


def test_graph_leg_merges_into_cited_context(runtime_db_url: str) -> None:
    question = "带投影的房型附近500米内有早餐的店"

    async def _body(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
        async with factory() as session:
            kb = await KnowledgeBaseRepository(session).create(
                code=f"graph-merge-{uuid4().hex[:8]}", name="图谱合流测试库"
            )
            await session.commit()
            await _index_document(
                factory, kb_id=kb.id, title="早餐说明", data=BREAKFAST_TEXT.encode()
            )

        stub = _StubGraphRetriever()
        async with factory() as session:
            retriever = KnowledgeRetriever(
                session,
                provider=DeterministicEmbeddings(),
                vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
                reranker=KeywordReranker(),
                graph_retriever=stub,  # type: ignore[arg-type]
            )
            context = await retriever.retrieve(question, top_n=2)
        return {
            "questions": stub.questions,
            "chunks": len(context.chunks),
            "is_empty": context.is_empty,
            "prompt_text": context.prompt_text,
            "citations": context.citations(),
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["questions"] == [question]
    assert values["chunks"] > 0
    assert values["is_empty"] is False
    assert "关系图补充事实" in values["prompt_text"]
    assert "[1]《早餐说明》第 " in values["prompt_text"]
    graph_start = values["chunks"] + 1
    assert f"[{graph_start}] 房型：亲子家庭房" in values["prompt_text"]
    assert f"[{graph_start + 1}] 周边：晨光咖啡（新区店）" in values["prompt_text"]

    citations = values["citations"]
    assert len(citations) == values["chunks"] + 2
    assert [item["index"] for item in citations] == list(range(1, len(citations) + 1))
    assert citations[graph_start - 1]["source"] == "graph"
    assert citations[graph_start - 1]["kind"] == "room_type"
    assert citations[graph_start]["kind"] == "place"


def test_graph_leg_keeps_context_non_empty_without_kb_match(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        retriever = KnowledgeRetriever(
            session,
            provider=DeterministicEmbeddings(),
            vector_store=FakeVectorStore.create(identity="dev", dimensions=64),
            reranker=KeywordReranker(),
            graph_retriever=_StubGraphRetriever(),  # type: ignore[arg-type]
        )
        context = await retriever.retrieve("带投影的房型附近500米内有早餐的店")
        return {
            "chunks": len(context.chunks),
            "is_empty": context.is_empty,
            "prompt_text": context.prompt_text,
            "citations": context.citations(),
        }

    values = run_with_session(runtime_db_url, _body)
    assert values["chunks"] == 0
    assert values["is_empty"] is False
    assert values["prompt_text"].startswith("关系图补充事实")
    assert [item["kind"] for item in values["citations"]] == ["room_type", "place"]
    assert [item["index"] for item in values["citations"]] == [1, 2]
