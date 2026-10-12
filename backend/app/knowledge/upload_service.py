"""知识库上传用例：原件入 MinIO、文档行与解析任务同事务落库，提交后才投递。

不变量：
- 同一知识库内 checksum 相同即视为重复内容（唯一索引裁决并发写入），不产生第二份原件；
- 对象先落 MinIO、任务后投递：处理器读原件时对象一定已存在；
- 文档行与 kb.parse 任务在同一事务提交——执行意图与事实不分离。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.parsing import SUPPORTED_SOURCE_TYPES
from app.knowledge.storage import ObjectStorage
from app.knowledge.task_handlers import HANDLER_VERSION, PARSE_TASK_TYPE, parse_dedupe_key
from app.models.knowledge import KbDocument
from app.repositories import KbDocumentRepository, KnowledgeBaseRepository
from app.services.task_service import TaskService, TaskSubmission

DEFAULT_EXTENSIONS: dict[str, str] = {"markdown": ".md", "text": ".txt", "pdf": ".pdf"}
CONTENT_TYPES: dict[str, str] = {
    "markdown": "text/markdown; charset=utf-8",
    "text": "text/plain; charset=utf-8",
    "pdf": "application/pdf",
}


class KnowledgeUploadError(Exception):
    """上传被拒绝：调用方据此回滚并映射为业务错误。"""


class KnowledgeBaseMissingError(KnowledgeUploadError):
    def __init__(self, kb_id: object) -> None:
        super().__init__(f"知识库不存在：{kb_id}")


class UnsupportedSourceTypeError(KnowledgeUploadError):
    def __init__(self, source_type: str) -> None:
        super().__init__(f"不支持的文档类型：{source_type or '未知'}")


class DocumentDuplicateError(KnowledgeUploadError):
    def __init__(self, document_id: UUID | None = None) -> None:
        super().__init__("同一知识库已存在相同内容的文档")
        self.document_id = document_id


@dataclass(frozen=True)
class UploadOutcome:
    document: KbDocument
    submission: TaskSubmission


class KnowledgeUploadService:
    def __init__(self, session: AsyncSession, *, storage: ObjectStorage | None = None) -> None:
        self._session = session
        self._storage = storage if storage is not None else ObjectStorage.from_settings()

    async def upload(
        self,
        *,
        kb_id: UUID,
        title: str,
        source_type: str,
        filename: str,
        data: bytes,
        uploaded_by: UUID | None = None,
    ) -> UploadOutcome:
        if source_type not in SUPPORTED_SOURCE_TYPES:
            raise UnsupportedSourceTypeError(source_type)
        if not data:
            raise KnowledgeUploadError("上传内容为空")
        if await KnowledgeBaseRepository(self._session).get_by_id(kb_id) is None:
            raise KnowledgeBaseMissingError(kb_id)

        documents = KbDocumentRepository(self._session)
        checksum = hashlib.sha256(data).hexdigest()
        existing = await documents.get_by_checksum(kb_id=kb_id, checksum=checksum)
        if existing is not None:
            raise DocumentDuplicateError(existing.id)

        object_key = f"{kb_id}/{uuid4().hex}{_extension(filename, source_type)}"
        await self._storage.put_bytes(
            object_key,
            data,
            content_type=CONTENT_TYPES.get(source_type, "application/octet-stream"),
        )
        document = await documents.create(
            kb_id=kb_id,
            title=title,
            source_type=source_type,
            object_key=object_key,
            size_bytes=len(data),
            checksum=checksum,
            uploaded_by=uploaded_by,
        )
        submission = await TaskService(self._session).submit(
            task_type=PARSE_TASK_TYPE,
            handler_version=HANDLER_VERSION,
            payload={"document_id": str(document.id)},
            dedupe_key=parse_dedupe_key(document.id),
        )
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            winner = await documents.get_by_checksum(kb_id=kb_id, checksum=checksum)
            raise DocumentDuplicateError(winner.id if winner is not None else None) from exc
        return UploadOutcome(document=document, submission=submission)


def _extension(filename: str, source_type: str) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    return suffix or DEFAULT_EXTENSIONS[source_type]
