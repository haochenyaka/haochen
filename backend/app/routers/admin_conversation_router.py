"""管理端会话监控：会话列表/详情、质检标签、转人工工作台。

权限：admin/manager/operator 可读，admin 可写标签和 handoff 操作。
operator 可操作 handoff 接单/关单。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.admin_conversation_service import AdminConversationService

router = APIRouter(prefix="/admin/conversations", tags=["admin-conversations"])

guard = require_roles("admin", "manager", "operator")
write_guard = require_roles("admin")
operator_guard = require_roles("admin", "operator")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]
OperatorGuardDep = Annotated[Identity, Depends(operator_guard)]


# ── Response models ──────────────────────────────────────────────


class ConversationSummaryModel(BaseModel):
    thread_id: str
    title: str | None = None
    agent_slug: str
    username: str
    user_display_name: str | None = None
    message_count: int = 0
    handoff_status: str | None = None
    labels: list[str] = []
    last_message_at: str | None = None
    created_at: str
    updated_at: str


class MessageViewModel(BaseModel):
    message_id: str
    seq: int
    role: str
    text: str
    run_id: str | None = None
    created_at: str
    meta: dict[str, object] | None = None


class LabelModel(BaseModel):
    id: int
    label: str
    source: str
    confidence: float | None = None
    comment: str | None = None
    created_by: str | None = None
    created_at: str


class ThreadDetailModel(BaseModel):
    thread_id: str
    title: str | None = None
    agent_slug: str
    username: str
    user_display_name: str | None = None
    created_at: str
    updated_at: str
    messages: list[MessageViewModel] = []
    labels: list[LabelModel] = []


class ConversationListResponse(BaseModel):
    items: list[ConversationSummaryModel]
    total: int


class SetLabelBody(BaseModel):
    label: str
    comment: str | None = None


class HandoffSummaryModel(BaseModel):
    id: str
    thread_id: str
    username: str
    reason: str | None = None
    status: str
    assigned_to_username: str | None = None
    assigned_at: str | None = None
    note: str | None = None
    closed_at: str | None = None
    created_at: str
    updated_at: str


class HandoffListResponse(BaseModel):
    items: list[HandoffSummaryModel]
    total: int


class AcceptHandoffBody(BaseModel):
    note: str | None = None


class CloseHandoffBody(BaseModel):
    note: str | None = None


class OperatorReplyBody(BaseModel):
    text: str


class OperatorReplyResponse(BaseModel):
    message_id: str
    thread_id: str
    text: str
    created_at: str


# ── Routes: Handoffs (must be before {thread_id} to avoid path collision) ──


@router.get("/handoffs/all", response_model=HandoffListResponse)
async def list_handoffs(
    session: SessionDep,
    _identity: GuardDep,
    status: str | None = Query(None, description="pending / accepted / closed"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> HandoffListResponse:
    svc = AdminConversationService(session)
    items, total = await svc.list_handoffs(status=status, offset=offset, limit=limit)
    return HandoffListResponse(
        items=[
            HandoffSummaryModel(
                id=item.id,
                thread_id=item.thread_id,
                username=item.username,
                reason=item.reason,
                status=item.status,
                assigned_to_username=item.assigned_to_username,
                assigned_at=item.assigned_at.isoformat() if item.assigned_at else None,
                note=item.note,
                closed_at=item.closed_at.isoformat() if item.closed_at else None,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
            for item in items
        ],
        total=total,
    )


@router.post("/handoffs/{handoff_id}/accept", response_model=HandoffSummaryModel)
async def accept_handoff(
    handoff_id: UUID,
    body: AcceptHandoffBody,
    session: SessionDep,
    identity: OperatorGuardDep,
) -> HandoffSummaryModel:
    svc = AdminConversationService(session)
    ok = await svc.accept_handoff(handoff_id, assigned_to=identity.user_id, assigned_to_username=identity.username or "")
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="转人工请求不存在")
    items, _ = await svc.list_handoffs(offset=0, limit=1)
    for item in items:
        if item.id == str(handoff_id):
            return HandoffSummaryModel(
                id=item.id,
                thread_id=item.thread_id,
                username=item.username,
                reason=item.reason,
                status=item.status,
                assigned_to_username=item.assigned_to_username,
                assigned_at=item.assigned_at.isoformat() if item.assigned_at else None,
                note=item.note,
                closed_at=item.closed_at.isoformat() if item.closed_at else None,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="接单后查询失败")


@router.post("/handoffs/{handoff_id}/close", response_model=HandoffSummaryModel)
async def close_handoff(
    handoff_id: UUID,
    body: CloseHandoffBody,
    session: SessionDep,
    _identity: OperatorGuardDep,
) -> HandoffSummaryModel:
    svc = AdminConversationService(session)
    ok = await svc.close_handoff(handoff_id, note=body.note)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="转人工请求不存在")
    items, _ = await svc.list_handoffs(offset=0, limit=1)
    for item in items:
        if item.id == str(handoff_id):
            return HandoffSummaryModel(
                id=item.id,
                thread_id=item.thread_id,
                username=item.username,
                reason=item.reason,
                status=item.status,
                assigned_to_username=item.assigned_to_username,
                assigned_at=item.assigned_at.isoformat() if item.assigned_at else None,
                note=item.note,
                closed_at=item.closed_at.isoformat() if item.closed_at else None,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="关单后查询失败")


# ── Routes: Operator Reply ───────────────────────────────────────


@router.post("/threads/{thread_id}/operator-reply", response_model=OperatorReplyResponse, status_code=status.HTTP_201_CREATED)
async def send_operator_reply(
    thread_id: UUID,
    body: OperatorReplyBody,
    session: SessionDep,
    identity: OperatorGuardDep,
) -> OperatorReplyResponse:
    if not body.text.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="回复内容不能为空")
    svc = AdminConversationService(session)
    msg = await svc.send_operator_reply(
        thread_id=thread_id,
        operator_user_id=identity.user_id,
        operator_username=identity.username or "operator",
        text=body.text.strip(),
    )
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话线程不存在")
    return OperatorReplyResponse(
        message_id=msg.message_id,
        thread_id=str(thread_id),
        text=msg.text,
        created_at=msg.created_at.isoformat(),
    )


# ── Routes: Conversations ────────────────────────────────────────


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    session: SessionDep,
    _identity: GuardDep,
    agent_slug: str | None = Query(None, description="按 Agent 筛选"),
    status: str | None = Query(None, description="handoff_pending"),
    label: str | None = Query(None, description="按质检标签筛选"),
    search: str | None = Query(None, description="搜索用户名"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> ConversationListResponse:
    svc = AdminConversationService(session)
    items, total = await svc.list_conversations(
        agent_slug=agent_slug,
        status=status,
        label=label,
        search=search,
        offset=offset,
        limit=limit,
    )
    return ConversationListResponse(
        items=[
            ConversationSummaryModel(
                thread_id=item.thread_id,
                title=item.title,
                agent_slug=item.agent_slug,
                username=item.username,
                user_display_name=item.user_display_name,
                message_count=item.message_count,
                handoff_status=item.handoff_status,
                labels=item.labels,
                last_message_at=item.last_message_at.isoformat() if item.last_message_at else None,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
            )
            for item in items
        ],
        total=total,
    )


@router.get("/{thread_id}", response_model=ThreadDetailModel)
async def get_thread_detail(
    thread_id: UUID,
    session: SessionDep,
    _identity: GuardDep,
) -> ThreadDetailModel:
    svc = AdminConversationService(session)
    detail = await svc.get_thread_detail(thread_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在")
    return ThreadDetailModel(
        thread_id=detail.thread_id,
        title=detail.title,
        agent_slug=detail.agent_slug,
        username=detail.username,
        user_display_name=detail.user_display_name,
        created_at=detail.created_at.isoformat(),
        updated_at=detail.updated_at.isoformat(),
        messages=[
            MessageViewModel(
                message_id=m.message_id,
                seq=m.seq,
                role=m.role,
                text=m.text,
                run_id=m.run_id,
                created_at=m.created_at.isoformat(),
                meta=m.meta,
            )
            for m in detail.messages
        ],
        labels=[
            LabelModel(
                id=lb["id"],
                label=lb["label"],
                source=lb["source"],
                confidence=lb["confidence"],
                comment=lb["comment"],
                created_by=lb["created_by"],
                created_at=lb["created_at"],
            )
            for lb in detail.labels
        ],
    )


# ── Routes: QA Labels ────────────────────────────────────────────


@router.post("/{thread_id}/labels", response_model=LabelModel, status_code=status.HTTP_201_CREATED)
async def set_label(
    thread_id: UUID,
    body: SetLabelBody,
    session: SessionDep,
    identity: WriteGuardDep,
) -> LabelModel:
    svc = AdminConversationService(session)
    created_by = identity.username or "admin"
    qa = await svc.set_label(
        thread_id=thread_id,
        label=body.label,
        source="manual",
        comment=body.comment,
        created_by=created_by,
    )
    return LabelModel(
        id=qa.id,
        label=qa.label,
        source=qa.source,
        confidence=qa.confidence,
        comment=qa.comment,
        created_by=qa.created_by,
        created_at=qa.created_at.isoformat(),
    )


@router.delete("/{thread_id}/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_label(
    thread_id: UUID,
    label_id: int,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> None:
    svc = AdminConversationService(session)
    removed = await svc.remove_label(label_id)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="标签不存在")