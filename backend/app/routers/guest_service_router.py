"""C 端客房服务：配送请求、配送跟踪、投诉提交。

权限：customer/guest 角色，只读本人资源（A6）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import ConflictError, DomainError, NotFoundError, PermissionDeniedError
from app.repositories.guest_repository import GuestRepository
from app.security import Identity, require_identity
from app.services.checkout_service import CheckoutService
from app.services.complaint_service import ComplaintService
from app.services.housekeeping_service import HousekeepingService
from app.services.robot_delivery_service import RobotDeliveryService

router = APIRouter(prefix="/guest/services", tags=["guest-services"])


class DeliveryRequest(BaseModel):
    item_name: str = Field(min_length=1, max_length=128)
    item_quantity: int = Field(default=1, ge=1, le=99)
    room_no: str | None = Field(default=None, min_length=1, max_length=32)


class DeliveryResponse(BaseModel):
    id: UUID
    room_no: str
    item: dict[str, Any]
    status: str
    external_delivery_id: str | None
    created_at: datetime
    finished_at: datetime | None


class CleanRequest(BaseModel):
    room_no: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=256)


class CleanResponse(BaseModel):
    id: UUID
    task_type: str
    room_no: str
    status: str
    created_at: datetime


class ComplaintBody(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    category: str | None = Field(default=None, max_length=32)
    order_no: str | None = Field(default=None, max_length=32)
    room_no: str | None = Field(default=None, max_length=32)


class ComplaintResponse(BaseModel):
    id: UUID
    status: str
    created_at: datetime


class CheckoutResponse(BaseModel):
    order_no: str
    status: str


IDEMPOTENCY_KEY_MAX = 64


async def _guest_id(session: AsyncSession, identity: Identity) -> UUID:
    guest = await GuestRepository(session).get_or_create_by_user(identity.user_id)
    return guest.id


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="客房服务异常")


@router.post("/deliveries", response_model=DeliveryResponse, status_code=status.HTTP_201_CREATED)
async def request_delivery(
    body: DeliveryRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> DeliveryResponse:
    """请求物品配送：创建 RobotDelivery 记录。"""
    guest_id = await _guest_id(session, identity)
    service = RobotDeliveryService(session)
    try:
        delivery = await service.create(
            guest_id=guest_id,
            room_no=body.room_no or "",
            item={"name": body.item_name, "quantity": body.item_quantity},
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return DeliveryResponse(
        id=delivery.id,
        room_no=delivery.room_no,
        item=dict(delivery.item),
        status=delivery.status,
        external_delivery_id=delivery.external_delivery_id,
        created_at=delivery.created_at,
        finished_at=delivery.finished_at,
    )


@router.get("/deliveries", response_model=list[DeliveryResponse])
async def list_my_deliveries(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
    limit: int = Query(default=20, ge=1, le=50),
) -> list[DeliveryResponse]:
    guest_id = await _guest_id(session, identity)
    service = RobotDeliveryService(session)
    deliveries = await service.list_for_guest(guest_id, limit=limit)
    return [
        DeliveryResponse(
            id=d.id,
            room_no=d.room_no,
            item=dict(d.item),
            status=d.status,
            external_delivery_id=d.external_delivery_id,
            created_at=d.created_at,
            finished_at=d.finished_at,
        )
        for d in deliveries
    ]


@router.get("/deliveries/{delivery_id}", response_model=DeliveryResponse)
async def get_delivery(
    delivery_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> DeliveryResponse:
    guest_id = await _guest_id(session, identity)
    service = RobotDeliveryService(session)
    try:
        delivery = await service.get_by_id(delivery_id)
    except DomainError as exc:
        _raise_http(exc)
    if delivery.guest_id != guest_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="配送不属于当前客人")
    return DeliveryResponse(
        id=delivery.id,
        room_no=delivery.room_no,
        item=dict(delivery.item),
        status=delivery.status,
        external_delivery_id=delivery.external_delivery_id,
        created_at=delivery.created_at,
        finished_at=delivery.finished_at,
    )


@router.post("/clean", response_model=CleanResponse, status_code=status.HTTP_201_CREATED)
async def request_clean(
    body: CleanRequest,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> CleanResponse:
    """请求呼叫清洁：创建 call_clean 任务。"""
    guest_id = await _guest_id(session, identity)
    from app.repositories.room_repository import RoomRepository

    room = await RoomRepository(session).get_by_room_no(body.room_no.strip())
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="房间不存在")
    hs = HousekeepingService(session)
    try:
        task = await hs.create(
            task_type="call_clean",
            room_id=room.id,
            room_no=room.room_no,
            payload={"requested_by": str(guest_id), "note": body.note} if body.note else None,
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return CleanResponse(
        id=task.id,
        task_type=task.task_type,
        room_no=task.room_no,
        status=task.status,
        created_at=task.created_at,
    )


@router.post("/complaints", response_model=ComplaintResponse, status_code=status.HTTP_201_CREATED)
async def submit_complaint(
    body: ComplaintBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> ComplaintResponse:
    guest_id = await _guest_id(session, identity)
    service = ComplaintService(session)
    try:
        complaint = await service.create(
            guest_id=guest_id,
            content=body.content,
            category=body.category,
            room_no=body.room_no,
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return ComplaintResponse(
        id=complaint.id,
        status=complaint.status,
        created_at=complaint.created_at,
    )


@router.post("/orders/{order_no}/checkout", response_model=CheckoutResponse)
async def initiate_checkout(
    order_no: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> CheckoutResponse:
    """客人发起退房：订单 → checkout_pending + 派 checkout_clean 任务。"""
    guest_id = await _guest_id(session, identity)
    service = CheckoutService(session)
    try:
        order = await service.initiate_checkout(guest_id=guest_id, order_no=order_no)
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    return CheckoutResponse(order_no=order.order_no, status=order.status)