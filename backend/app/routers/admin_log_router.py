"""智能体日志管理接口：跨用户日志列表（筛选/分页）、运行日志详情、批量软删与留痕。

权限：admin/manager 只读；批量删除仅 admin。软删不影响报表（验收主张 A4）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.repositories.audit_repository import AuditRepository
from app.security import Identity, require_roles
from app.services.admin_log_service import AdminLogService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/logs", tags=["admin-logs"])

read_guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ReadGuardDep = Annotated[Identity, Depends(read_guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]


class LogEntryResponse(BaseModel):
    kind: str
    id: int
    run_id: str
    seq: int
    agent_slug: str
    status: str
    created_at: datetime
    duration_ms: int | None = None
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieval_ref_count: int | None = None
    tool_name: str | None = None
    args_digest: dict[str, object] | None = None
    idempotency_key: str | None = None
    deleted_at: datetime | None = None
    guest_username: str | None = None
    guest_nickname: str | None = None


class LogListResponse(BaseModel):
    entries: list[LogEntryResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


class AuditEntryResponse(BaseModel):
    """单条审计条目（复用 AuditRepository.list_timeline 输出）。"""

    kind: str
    id: int
    run_id: str
    seq: int
    agent_slug: str
    status: str
    created_at: datetime
    duration_ms: int | None = None
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieval_refs: list[dict[str, object]] | None = None
    tool_name: str | None = None
    args_digest: dict[str, object] | None = None
    idempotency_key: str | None = None


class AuditTimelineResponse(BaseModel):
    entries: list[AuditEntryResponse]
    truncated: bool


class SoftDeleteRequest(BaseModel):
    model_ids: list[int] = Field(default_factory=list, description="待删除模型审计行 id")
    tool_ids: list[int] = Field(default_factory=list, description="待删除工具审计行 id")


class SoftDeleteResponse(BaseModel):
    model_affected: int
    tool_affected: int
    total_affected: int
    log_id: int


class AdminActionLogResponse(BaseModel):
    id: int
    actor_user_id: UUID
    actor_username: str
    actor_role: str
    action: str
    target_ids: list[object]
    target_count: int
    detail: dict[str, object] | None = None
    created_at: datetime


@router.get("", response_model=LogListResponse)
async def list_logs(
    session: SessionDep,
    _identity: ReadGuardDep,
    start: Annotated[datetime | None, Query(description="起始（含），ISO8601")] = None,
    end: Annotated[datetime | None, Query(description="结束（不含），ISO8601")] = None,
    kind: Annotated[str | None, Query(description="model 或 tool")] = None,
    agent_slug: Annotated[str | None, Query(description="Agent 标识")] = None,
    status: Annotated[str | None, Query(description="调用状态")] = None,
    run_id: Annotated[str | None, Query(description="运行 id")] = None,
    include_deleted: Annotated[bool, Query(description="是否包含软删行")] = False,
    page: Annotated[int, Query(ge=1, description="页码")] = 1,
    page_size: Annotated[int, Query(ge=1, le=200, description="每页条数")] = 50,
) -> LogListResponse:
    svc = AdminLogService(session)
    result = await svc.list_logs(
        start=start, end=end, kind=kind, agent_slug=agent_slug,
        status=status, run_id=run_id, include_deleted=include_deleted,
        page=page, page_size=page_size,
    )
    return LogListResponse(
        entries=[LogEntryResponse(**entry.__dict__) for entry in result.entries],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        has_more=result.has_more,
    )


@router.get("/runs/{run_id}/timeline", response_model=AuditTimelineResponse)
async def get_run_timeline(
    run_id: UUID,
    session: SessionDep,
    _identity: ReadGuardDep,
    limit: int = Query(500, ge=1, le=1000, description="最大返回条数"),
) -> AuditTimelineResponse:
    repo = AuditRepository(session)
    timeline = await repo.list_timeline(run_id=run_id, limit=limit, include_deleted=True)
    return AuditTimelineResponse(
        entries=[AuditEntryResponse(
            kind=e.kind, id=e.id, run_id=e.run_id, seq=e.seq,
            agent_slug=e.agent_slug, status=e.status, created_at=e.created_at,
            duration_ms=e.duration_ms, error=e.error, model=e.model,
            prompt_tokens=e.prompt_tokens, cached_prompt_tokens=e.cached_prompt_tokens,
            completion_tokens=e.completion_tokens, retrieval_refs=e.retrieval_refs,
            tool_name=e.tool_name, args_digest=e.args_digest,
            idempotency_key=e.idempotency_key,
        ) for e in timeline.entries],
        truncated=timeline.truncated,
    )


@router.delete("/delete", status_code=status.HTTP_200_OK, response_model=SoftDeleteResponse)
async def soft_delete_audit_logs(
    body: SoftDeleteRequest,
    session: SessionDep,
    identity: WriteGuardDep,
) -> SoftDeleteResponse:
    if not body.model_ids and not body.tool_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model_ids 和 tool_ids 至少提供一个",
        )
    svc = AdminLogService(session)
    result = await svc.soft_delete(
        model_ids=body.model_ids,
        tool_ids=body.tool_ids,
        actor_user_id=identity.user_id,
        actor_username=identity.username,
        actor_role=identity.role,
    )
    await session.commit()
    logger.info(
        "审计日志软删：actor=%s model=%d tool=%d log_id=%d",
        identity.username, result.model_affected, result.tool_affected, result.log_id,
    )
    return SoftDeleteResponse(
        model_affected=result.model_affected,
        tool_affected=result.tool_affected,
        total_affected=result.total_affected,
        log_id=result.log_id,
    )


@router.get("/action-logs", response_model=list[AdminActionLogResponse])
async def list_action_logs(
    session: SessionDep,
    _identity: ReadGuardDep,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
) -> list[AdminActionLogResponse]:
    svc = AdminLogService(session)
    logs = await svc.get_action_logs(page=page, page_size=page_size)
    return [
        AdminActionLogResponse(
            id=row.id,
            actor_user_id=row.actor_user_id,
            actor_username=row.actor_username,
            actor_role=row.actor_role,
            action=row.action,
            target_ids=list(row.target_ids),
            target_count=row.target_count,
            detail=row.detail,
            created_at=row.created_at,
        )
        for row in logs
    ]