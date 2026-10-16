"""保洁端任务接口：只读列表/详情 + 状态迁移（accept/start/finish/verify/close）。

权限：staff 角色；跨员工任务访问一律 403（A6）。
退房清洁（checkout_clean）进入 verified 时自动触发门锁凭证吊销与订单完成。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain import HOUSEKEEPING_STATUSES
from app.domain.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.lock.client import LockClient
from app.security import Identity, require_roles
from app.services.checkout_service import CheckoutService
from app.services.housekeeping_service import HousekeepingService
from app.services.staff_task_service import DEFAULT_TASK_LIMIT, MAX_TASK_LIMIT, StaffTaskService

router = APIRouter(prefix="/staff", tags=["staff-tasks"])

staff_guard = require_roles("staff")


class TaskResponse(BaseModel):
    id: UUID
    task_type: str
    room_id: UUID
    room_no: str
    order_id: UUID | None
    assigned_staff_id: UUID | None
    status: str
    payload: dict[str, object] | None
    evidence: dict[str, object] | None
    due_at: datetime | None
    accepted_at: datetime | None
    started_at: datetime | None
    done_at: datetime | None
    verified_at: datetime | None
    escalated_at: datetime | None
    created_at: datetime


class TransitionBody(BaseModel):
    evidence: dict[str, Any] | None = None


class AcceptBody(BaseModel):
    pass


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="任务用例异常")


def _validate_status(value: str) -> None:
    if value not in HOUSEKEEPING_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="任务状态取值非法"
        )


@router.get("/tasks", response_model=list[TaskResponse])
async def list_my_tasks(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
    task_status: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_TASK_LIMIT)] = DEFAULT_TASK_LIMIT,
) -> list[TaskResponse]:
    if task_status is not None:
        _validate_status(task_status)
    service = StaffTaskService(session)
    try:
        tasks = await service.list_my_tasks(
            identity.user_id,
            statuses=(task_status,) if task_status is not None else None,
            limit=limit,
        )
    except DomainError as exc:
        _raise_http(exc)
    return [TaskResponse(**vars(task)) for task in tasks]


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_my_task(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    service = StaffTaskService(session)
    try:
        task = await service.get_my_task(identity.user_id, task_id)
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))


@router.post("/tasks/{task_id}/accept", response_model=TaskResponse)
async def accept_task(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    """接单：pending → accepted，将任务绑定到当前员工。"""
    service = StaffTaskService(session)
    profile = await service.resolve_profile(identity.user_id)
    try:
        task = await service.get_my_task(identity.user_id, task_id)
    except DomainError:
        pass  # 未指派的任务可接
    hs = HousekeepingService(session)
    try:
        task = await hs.transition(
            task_id,
            "accepted",
            assigned_staff_id=profile.id,
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))


@router.post("/tasks/{task_id}/start", response_model=TaskResponse)
async def start_task(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    """开始清洁：accepted → in_progress。"""
    hs = HousekeepingService(session)
    service = StaffTaskService(session)
    try:
        _ = await service.get_my_task(identity.user_id, task_id)
        task = await hs.transition(task_id, "in_progress")
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))


@router.post("/tasks/{task_id}/finish", response_model=TaskResponse)
async def finish_task(
    task_id: UUID,
    body: TransitionBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    """完成清洁：in_progress → done，可提交留证。"""
    hs = HousekeepingService(session)
    service = StaffTaskService(session)
    try:
        _ = await service.get_my_task(identity.user_id, task_id)
        task = await hs.transition(task_id, "done", evidence=body.evidence)
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))


@router.post("/tasks/{task_id}/verify", response_model=TaskResponse)
async def verify_task(
    task_id: UUID,
    body: TransitionBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    """验收清洁：done → verified。

    退房清洁（checkout_clean）须携带 password_rotated=true。
    验证通过后自动触发门锁凭证吊销与订单完成。
    """
    hs = HousekeepingService(session)
    service = StaffTaskService(session)
    try:
        task = await service.get_my_task(identity.user_id, task_id)
        task = await hs.transition(task_id, "verified", evidence=body.evidence)

        # 退房清洁验证通过后，吊销门锁凭证并完成退房
        if task.task_type == "checkout_clean":
            lock_client = LockClient.from_settings()
            checkout = CheckoutService(session)
            await checkout.verify_and_complete(task_id=task_id, lock_client=lock_client)

        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))


@router.post("/tasks/{task_id}/close", response_model=TaskResponse)
async def close_task(
    task_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(staff_guard)],
) -> TaskResponse:
    """关闭任务：verified → closed。"""
    hs = HousekeepingService(session)
    service = StaffTaskService(session)
    try:
        _ = await service.get_my_task(identity.user_id, task_id)
        task = await hs.transition(task_id, "closed")
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return TaskResponse(**vars(task))
