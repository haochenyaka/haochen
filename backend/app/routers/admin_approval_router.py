"""工具审批管理接口：待审批列表与批准/拒绝。

权限：admin/manager。决定落库后投递恢复（业务在服务层）。

- GET  /api/admin/approvals/pending  列出待审批
- POST /api/admin/approvals/{id}/decide  批准/拒绝并触发恢复
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.security import Identity, require_roles
from app.services.run_dispatch import NullRunDispatcher, RunDispatcher
from app.services.tool_approval_service import ApprovalView, ToolApprovalService

router = APIRouter(prefix="/admin/approvals", tags=["admin-approvals"])

guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]

_fallback_dispatcher = NullRunDispatcher()


def get_run_dispatcher(request: Request) -> RunDispatcher:
    dispatcher: RunDispatcher | None = getattr(request.app.state, "run_dispatcher", None)
    return dispatcher if dispatcher is not None else _fallback_dispatcher


class ApprovalModel(BaseModel):
    id: str
    run_id: str
    thread_id: str
    tool_name: str
    call_id: str
    reason: str | None
    args_digest: dict[str, object] | None
    status: str
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime


class DecideBody(BaseModel):
    approved: bool


def _to_model(view: ApprovalView) -> ApprovalModel:
    return ApprovalModel(
        id=view.id,
        run_id=view.run_id,
        thread_id=view.thread_id,
        tool_name=view.tool_name,
        call_id=view.call_id,
        reason=view.reason,
        args_digest=view.args_digest,
        status=view.status,
        decided_by=view.decided_by,
        decided_at=view.decided_at,
        created_at=view.created_at,
    )


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="审批用例异常")


@router.get("/pending", response_model=list[ApprovalModel])
async def list_pending(
    session: SessionDep,
    _identity: GuardDep,
) -> list[ApprovalModel]:
    views = await ToolApprovalService(session).list_pending()
    return [_to_model(view) for view in views]


@router.post("/{approval_id}/decide", response_model=ApprovalModel)
async def decide(
    approval_id: UUID,
    body: DecideBody,
    session: SessionDep,
    dispatcher: Annotated[RunDispatcher, Depends(get_run_dispatcher)],
    identity: GuardDep,
) -> ApprovalModel:
    try:
        view = await ToolApprovalService(session).decide(
            approval_id=approval_id,
            approved=body.approved,
            decided_by=identity.user_id,
            dispatcher=dispatcher,
        )
    except DomainError as exc:
        _raise_http(exc)
    return _to_model(view)
