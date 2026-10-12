"""知识库管理接口：上传、读取、重试（检索侧 query_kb 在 P3-T5 接入）。

权限：读为 admin/manager，写为 admin。原件与解析产物经服务端中转读取，不向调用方
暴露对象存储地址；文档处理进度以 kb_documents 状态为准（任务结果只作参考）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.domain import KB_INDEX_CLAIM_STATUSES, KB_PARSE_CLAIM_STATUSES, kb_document_is_active
from app.knowledge.parsing import source_type_for_filename
from app.knowledge.storage import ObjectStorage
from app.knowledge.task_handlers import (
    HANDLER_VERSION,
    INDEX_TASK_TYPE,
    PARSE_TASK_TYPE,
    index_dedupe_key,
    parse_dedupe_key,
)
from app.knowledge.upload_service import (
    DocumentDuplicateError,
    KnowledgeBaseMissingError,
    KnowledgeUploadError,
    KnowledgeUploadService,
    UnsupportedSourceTypeError,
)
from app.models.knowledge import KbDocument
from app.repositories import KbChunkRepository, KbDocumentRepository, KnowledgeBaseRepository
from app.security import Identity, require_roles
from app.services.eval_invalidation_service import invalidate_old_reports_on_content_change
from app.services.task_dispatch import NullTaskDispatcher, TaskDispatcher, dispatch_task
from app.services.task_service import TaskService

router = APIRouter(prefix="/admin", tags=["admin-knowledge"])

read_guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

_fallback_dispatcher = NullTaskDispatcher()
UPLOAD_CHUNK_BYTES = 1024 * 1024
PREVIEW_CHUNK_LIMIT = 3
PREVIEW_CHARS = 200


def get_task_dispatcher(request: Request) -> TaskDispatcher:
    dispatcher: TaskDispatcher | None = getattr(request.app.state, "task_dispatcher", None)
    return dispatcher if dispatcher is not None else _fallback_dispatcher


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    code: str
    name: str
    description: str | None
    scope: str
    active: bool


class DocumentResponse(BaseModel):
    id: UUID
    kb_id: UUID
    title: str
    source_type: str
    size_bytes: int
    status: str
    chunk_count: int
    error: str | None
    attempt_count: int
    created_at: datetime
    parsed_at: datetime | None
    indexed_at: datetime | None


class ChunkPreview(BaseModel):
    ordinal: int
    char_start: int | None
    char_end: int | None
    content: str


class DocumentDetailResponse(DocumentResponse):
    preview: list[ChunkPreview]


class DocumentUploadResponse(BaseModel):
    document: DocumentResponse
    task_id: UUID


class ReprocessResponse(BaseModel):
    document_id: UUID
    task_type: str
    task_id: UUID
    deduplicated: bool


def _document_response(document: KbDocument) -> DocumentResponse:
    return DocumentResponse(
        **{key: getattr(document, key) for key in DocumentResponse.model_fields}
    )


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseResponse])
async def list_knowledge_bases(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
    include_inactive: bool = False,
) -> list[KnowledgeBaseResponse]:
    rows = await KnowledgeBaseRepository(session).list_all(active_only=not include_inactive)
    return [
        KnowledgeBaseResponse(
            id=row.id,
            code=row.code,
            name=row.name,
            description=row.description,
            scope=row.scope,
            active=row.active,
        )
        for row in rows
    ]


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=list[DocumentResponse],
)
async def list_documents(
    kb_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
    status_filter: str | None = None,
) -> list[DocumentResponse]:
    if await KnowledgeBaseRepository(session).get_by_id(kb_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在")
    rows = await KbDocumentRepository(session).list_by_kb(kb_id, status=status_filter)
    return [_document_response(row) for row in rows]


@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    kb_id: UUID,
    file: Annotated[UploadFile, File()],
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(write_guard)],
    dispatcher: Annotated[TaskDispatcher, Depends(get_task_dispatcher)],
    title: Annotated[str | None, Form(max_length=200)] = None,
) -> DocumentUploadResponse:
    filename = file.filename or ""
    source_type = source_type_for_filename(filename)
    if source_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="不支持的文档类型：仅接受 .md / .markdown / .txt / .pdf",
        )
    data = await _read_upload(file, settings_max=get_settings().kb_max_upload_bytes)
    service = KnowledgeUploadService(session)
    try:
        outcome = await service.upload(
            kb_id=kb_id,
            title=(title or _default_title(filename)).strip() or _default_title(filename),
            source_type=source_type,
            filename=filename,
            data=data,
            uploaded_by=identity.user_id,
        )
    except DocumentDuplicateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{exc}：已有文档 {exc.document_id}",
        ) from exc
    except KnowledgeBaseMissingError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UnsupportedSourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except KnowledgeUploadError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    # 知识库内容变更 → 旧评估基线作废（异步，失败不阻断上传）
    await invalidate_old_reports_on_content_change(session, reason="content_changed")

    await dispatch_task(
        dispatcher, outcome.submission.task_id if outcome.submission.should_dispatch else None
    )
    return DocumentUploadResponse(
        document=_document_response(outcome.document), task_id=outcome.submission.task_id
    )


@router.get("/kb-documents/{document_id}", response_model=DocumentDetailResponse)
async def get_document(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
) -> DocumentDetailResponse:
    document = await _require_document(session, document_id)
    chunks = await KbChunkRepository(session).list_by_document(document_id)
    return DocumentDetailResponse(
        **_document_response(document).model_dump(),
        preview=[
            ChunkPreview(
                ordinal=chunk.ordinal,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
                content=chunk.content[:PREVIEW_CHARS],
            )
            for chunk in chunks[:PREVIEW_CHUNK_LIMIT]
        ],
    )


@router.get("/kb-documents/{document_id}/original")
async def get_document_original(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
) -> Response:
    document = await _require_document(session, document_id)
    data = await ObjectStorage.from_settings().get_bytes(document.object_key)
    return Response(content=data, media_type="application/octet-stream")


@router.post("/kb-documents/{document_id}/reprocess", response_model=ReprocessResponse)
async def reprocess_document(
    document_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
    dispatcher: Annotated[TaskDispatcher, Depends(get_task_dispatcher)],
) -> ReprocessResponse:
    """按当前状态重新排队：已索引/解析失败重走解析，索引失败重走索引。"""
    document = await _require_document(session, document_id)
    if kb_document_is_active(document.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"文档正在处理中：{document.status}"
        )
    if document.status in KB_INDEX_CLAIM_STATUSES:
        task_type, dedupe_key = INDEX_TASK_TYPE, index_dedupe_key(document_id)
    elif document.status in KB_PARSE_CLAIM_STATUSES:
        task_type, dedupe_key = PARSE_TASK_TYPE, parse_dedupe_key(document_id)
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"当前状态不可重试：{document.status}"
        )

    submission = await TaskService(session).submit(
        task_type=task_type,
        handler_version=HANDLER_VERSION,
        payload={"document_id": str(document_id)},
        dedupe_key=dedupe_key,
    )
    await session.commit()
    await dispatch_task(dispatcher, submission.task_id if submission.should_dispatch else None)
    return ReprocessResponse(
        document_id=document_id,
        task_type=task_type,
        task_id=submission.task_id,
        deduplicated=submission.deduplicated,
    )


async def _require_document(session: AsyncSession, document_id: UUID) -> KbDocument:
    document = await KbDocumentRepository(session).get_by_id(document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    return document


async def _read_upload(file: UploadFile, *, settings_max: int) -> bytes:
    buffer = bytearray()
    while True:
        chunk = await file.read(UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > settings_max:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"文件超过上限 {settings_max} 字节",
            )
    return bytes(buffer)


def _default_title(filename: str) -> str:
    stem = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return stem.rsplit(".", 1)[0] or "未命名文档"
