"""知识库外部依赖替身：对象存储、向量库与任务投递记录器（集成测试共用）。"""

from __future__ import annotations

from typing import ClassVar
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.knowledge.milvus_store import ChunkVector, VectorStoreError
from app.knowledge.tokenization import tokenize
from app.knowledge.upload_service import KnowledgeUploadService
from app.repositories import KbDocumentRepository, KnowledgeBaseRepository
from app.services.task_service import TaskExecutor


class FakeObjectStorage:
    """进程内对象存储替身：类级字典保证注入到处理器与用例的是同一份对象。"""

    objects: ClassVar[dict[str, bytes]] = {}

    def __init__(self) -> None:
        self._objects = FakeObjectStorage.objects

    @classmethod
    def from_settings(cls) -> FakeObjectStorage:
        return cls()

    @classmethod
    def reset(cls) -> None:
        cls.objects = {}

    async def put_bytes(
        self, object_key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> None:
        self._objects[object_key] = data

    async def get_bytes(self, object_key: str) -> bytes:
        return self._objects[object_key]

    async def remove(self, object_key: str) -> None:
        self._objects.pop(object_key, None)


class FakeVectorStore:
    rows: ClassVar[dict[str, dict[str, ChunkVector]]] = {}
    fail_upsert: ClassVar[bool] = False

    def __init__(self, *, collection: str = "fake", dimensions: int = 0) -> None:
        self._collection = collection
        self._dimensions = dimensions

    @classmethod
    def create(cls, *, identity: str, dimensions: int = 0) -> FakeVectorStore:
        return cls(collection=f"fake_{identity}", dimensions=dimensions)

    @classmethod
    def reset(cls) -> None:
        cls.rows = {}
        cls.fail_upsert = False

    @property
    def collection(self) -> str:
        return self._collection

    def documents(self, document_id: UUID) -> list[ChunkVector]:
        return list(FakeVectorStore.rows.get(str(document_id), {}).values())

    async def ensure_collection(self, *, dimensions: int) -> None:
        self._dimensions = dimensions

    async def upsert(self, vectors: list[ChunkVector]) -> int:
        if FakeVectorStore.fail_upsert:
            raise VectorStoreError("向量写入失败：替身注入故障")
        bucket = FakeVectorStore.rows.setdefault(
            vectors[0].document_id if vectors else "unknown", {}
        )
        for vector in vectors:
            bucket[vector.chunk_id] = vector
        return len(vectors)

    async def delete_document(self, document_id: str) -> None:
        FakeVectorStore.rows.pop(document_id, None)

    async def hybrid_search(
        self,
        *,
        query_text: str,
        query_vector: list[float],
        limit: int,
        kb_ids: list[str] | None = None,
        document_ids: list[str] | None = None,
    ) -> list[tuple[str, float]]:
        """替身宽捞：词元覆盖率排序，只保证「命中集合与过滤条件」语义，不模拟 RRF 数值。"""
        query_tokens = set(tokenize(query_text))
        hits: list[tuple[str, float]] = []
        for bucket in FakeVectorStore.rows.values():
            for vector in bucket.values():
                if kb_ids and vector.kb_id not in kb_ids:
                    continue
                if document_ids and vector.document_id not in document_ids:
                    continue
                overlap = len(query_tokens & set(tokenize(vector.text)))
                if not overlap:
                    continue
                hits.append((vector.chunk_id, overlap / max(1, len(query_tokens))))
        hits.sort(key=lambda pair: pair[1], reverse=True)
        return hits[:limit]


class RecordingTaskDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_task(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def aclose(self) -> None:
        return None


def fake_upload_service(session: AsyncSession) -> KnowledgeUploadService:
    """种子/用例装配替身：对象存储走进程内字典，不依赖 MinIO。"""
    return KnowledgeUploadService(session, storage=FakeObjectStorage())


async def create_knowledge_base(
    factory: async_sessionmaker[AsyncSession],
    *,
    code: str,
    scope: str = "guest",
    active: bool = True,
) -> UUID:
    """建库（提交返回 id）：调用方在独立会话里写入，避免跨用例状态残留。"""
    async with factory() as session:
        kb = await KnowledgeBaseRepository(session).create(
            code=code, name=f"测试库 {code}", scope=scope
        )
        if not active:
            kb.active = False
        await session.commit()
        return kb.id


async def index_document(
    factory: async_sessionmaker[AsyncSession], *, kb_id: UUID, title: str, data: bytes
) -> UUID:
    """上传 → 解析 → 索引全链路（走真实任务执行器与替身外部依赖），返回文档 id。"""
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
