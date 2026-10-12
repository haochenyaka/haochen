"""C 端订单：下单、列表、详情、改期、续订、取消、发票申请与支付发起。

不变量：
- 归属与状态复验在服务层（A6）：不存在的订单与不可见资源同为 404，非归属操作（他人订单）返回 403；
- 下单幂等键由服务端按客人命名空间化（`guest:{guest_id}:{客户端键}`），他人的键不可能命中，
  重放只返回本人既有订单；
- 写操作成功后返回变更后的订单详情，前端一次往返即可刷新；
- 续订保持当前凭证（只展期窗口），门锁不可达即失败关闭——不出现「订单已续、密码失效」；
- 渠道失败区分明确失败（502）与不可达（503），两种失败都不落支付事实，客人可重试；
- 响应只含元数据：凭证给版本与生效窗口，永不带明文（A2）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Annotated, Any, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.lock.service import LockCredentialService
from app.models.domain import Invoice, Order, RoomType
from app.repositories import (
    GuestRepository,
    InvoiceRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
)
from app.routers.guest_auth_router import get_wechat_client
from app.security import Identity, require_identity
from app.services.booking_service import renew_booking_with_credential
from app.services.order_service import OrderService
from app.services.payment_service import PaymentService
from app.wechat import WechatClient, WechatError, WechatUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest/orders", tags=["guest-orders"])

ORDER_LIST_LIMIT_DEFAULT = 20
ORDER_LIST_LIMIT_MAX = 50
CLIENT_IDEMPOTENCY_KEY_MAX = 64


class PayInitiationResponse(BaseModel):
    order_no: str
    amount_fen: int
    prepay_id: str
    pay_params: dict[str, object]


class OrderCreateBody(BaseModel):
    room_type_code: str = Field(min_length=1, max_length=32)
    check_in_date: date
    check_out_date: date
    guest_count: int = Field(default=1, ge=1, le=16)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=CLIENT_IDEMPOTENCY_KEY_MAX,
        description="客户端为一次提交生成的键；重试沿用同一键即回放本人既有订单",
    )


class RescheduleBody(BaseModel):
    check_in_date: date
    check_out_date: date


class RenewBody(BaseModel):
    check_out_date: date


class CancelBody(BaseModel):
    reason: str | None = Field(default=None, max_length=128)


class InvoiceBody(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    tax_no: str | None = Field(default=None, max_length=64)


class OrderSummary(BaseModel):
    order_no: str
    status: str
    room_type_code: str | None
    room_type_name: str | None
    check_in_date: date
    check_out_date: date
    nights: int
    amount_fen: int
    paid_amount_fen: int | None
    room_no: str | None
    created_at: datetime


class CredentialSummary(BaseModel):
    version: int
    reason: str
    valid_from: datetime
    valid_to: datetime


class InvoiceSummary(BaseModel):
    id: UUID
    title: str
    tax_no: str | None
    amount_fen: int
    status: str
    created_at: datetime
    issued_at: datetime | None


class OrderDetail(OrderSummary):
    paid_at: datetime | None
    checked_in_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None
    floor: int | None
    credential: CredentialSummary | None
    invoice: InvoiceSummary | None
    events: list[dict[str, Any]]


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, PermissionDeniedError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, ConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, ValidationFailedError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="订单用例异常")


async def _guest_id(session: AsyncSession, identity: Identity) -> UUID:
    guest = await GuestRepository(session).get_or_create_by_user(identity.user_id)
    return guest.id


def _summary_fields(
    order: Order, room_type: RoomType | None, room_no: str | None
) -> dict[str, Any]:
    return {
        "order_no": order.order_no,
        "status": order.status,
        "room_type_code": room_type.code if room_type is not None else None,
        "room_type_name": room_type.name if room_type is not None else None,
        "check_in_date": order.check_in_date,
        "check_out_date": order.check_out_date,
        "nights": order.nights,
        "amount_fen": order.amount_fen,
        "paid_amount_fen": order.paid_amount_fen,
        "room_no": room_no,
        "created_at": order.created_at,
    }


async def _lookup_summary(session: AsyncSession, order: Order) -> dict[str, Any]:
    room_type = await RoomTypeRepository(session).get_by_id(order.room_type_id)
    room_no: str | None = None
    if order.room_id is not None:
        room = await RoomRepository(session).get_by_id(order.room_id)
        room_no = room.room_no if room is not None else None
    return _summary_fields(order, room_type, room_no)


async def _detail(session: AsyncSession, *, guest_id: UUID, order_no: str) -> OrderDetail:
    """订单详情：归属校验沿用「不存在」口径；凭证只给元数据（永不带明文）。"""
    overview = await LockCredentialService(session).overview_for_order(
        guest_id=guest_id, order_no=order_no
    )
    order = overview.order
    invoice = await InvoiceRepository(session).get_by_order(order.id)
    credential = overview.credential
    return OrderDetail(
        **await _lookup_summary(session, order),
        paid_at=order.paid_at,
        checked_in_at=order.checked_in_at,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
        cancel_reason=order.cancel_reason,
        floor=overview.floor,
        credential=CredentialSummary(
            version=credential.version,
            reason=credential.reason,
            valid_from=credential.valid_from,
            valid_to=credential.valid_to,
        )
        if credential is not None
        else None,
        invoice=_invoice_summary(invoice) if invoice is not None else None,
        events=list(order.events or []),
    )


def _invoice_summary(invoice: Invoice) -> InvoiceSummary:
    return InvoiceSummary(
        id=invoice.id,
        title=invoice.title,
        tax_no=invoice.tax_no,
        amount_fen=invoice.amount_fen,
        status=invoice.status,
        created_at=invoice.created_at,
        issued_at=invoice.issued_at,
    )


@router.get("", response_model=list[OrderSummary])
async def list_my_orders(
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
    limit: int = Query(default=ORDER_LIST_LIMIT_DEFAULT, ge=1, le=ORDER_LIST_LIMIT_MAX),
) -> list[OrderSummary]:
    guest_id = await _guest_id(session, identity)
    orders = await OrderRepository(session).list_recent_for_guest(guest_id, limit=limit)
    types = {
        row.id: row for row in await RoomTypeRepository(session).list_all(include_inactive=True)
    }
    rooms = {row.id: row.room_no for row in await RoomRepository(session).list_all()}
    await session.commit()
    return [
        OrderSummary(
            **_summary_fields(
                order,
                types.get(order.room_type_id),
                rooms.get(order.room_id) if order.room_id is not None else None,
            )
        )
        for order in orders
    ]


@router.get("/{order_no}", response_model=OrderDetail)
async def get_my_order(
    order_no: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> OrderDetail:
    guest_id = await _guest_id(session, identity)
    try:
        detail = await _detail(session, guest_id=guest_id, order_no=order_no)
    except DomainError as exc:
        _raise_http(exc)
    await session.commit()
    return detail


@router.post("", response_model=OrderDetail, status_code=status.HTTP_201_CREATED)
async def create_my_order(
    body: OrderCreateBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> OrderDetail:
    """下单：房型、日期窗、容量与库存执行处复验；客户端幂等键按客人命名空间化。

    重放（同一客人同一键）返回既有订单并以 200 区分；键含客人 id，他人键不可能命中。
    """
    guest_id = await _guest_id(session, identity)
    idempotency_key = (
        f"guest:{guest_id}:{body.idempotency_key}" if body.idempotency_key is not None else None
    )
    try:
        replayed = (
            await OrderRepository(session).get_by_idempotency_key(idempotency_key)
            if idempotency_key is not None
            else None
        )
        order = await OrderService(session).create_booking(
            guest_id=guest_id,
            room_type_code=body.room_type_code,
            check_in_date=body.check_in_date,
            check_out_date=body.check_out_date,
            guest_count=body.guest_count,
            idempotency_key=idempotency_key,
        )
        await session.commit()
        detail = await _detail(session, guest_id=guest_id, order_no=order.order_no)
    except DomainError as exc:
        _raise_http(exc)
    await session.commit()
    if replayed is not None:
        response.status_code = status.HTTP_200_OK
    return detail


@router.post("/{order_no}/reschedule", response_model=OrderDetail)
async def reschedule_my_order(
    order_no: str,
    body: RescheduleBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> OrderDetail:
    guest_id = await _guest_id(session, identity)
    try:
        await OrderService(session).reschedule_booking(
            guest_id=guest_id,
            order_no=order_no,
            new_check_in_date=body.check_in_date,
            new_check_out_date=body.check_out_date,
        )
        await session.commit()
        detail = await _detail(session, guest_id=guest_id, order_no=order_no)
    except DomainError as exc:
        _raise_http(exc)
    await session.commit()
    return detail


@router.post("/{order_no}/renew", response_model=OrderDetail)
async def renew_my_order(
    order_no: str,
    body: RenewBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> OrderDetail:
    """续订：延长退房日期并保持当前凭证（只展期窗口）；门锁不可达即失败关闭。"""
    guest_id = await _guest_id(session, identity)
    try:
        await renew_booking_with_credential(
            session,
            guest_id=guest_id,
            order_no=order_no,
            new_check_out_date=body.check_out_date,
        )
        await session.commit()
        detail = await _detail(session, guest_id=guest_id, order_no=order_no)
    except DomainError as exc:
        _raise_http(exc)
    await session.commit()
    return detail


@router.post("/{order_no}/cancel", response_model=OrderDetail)
async def cancel_my_order(
    order_no: str,
    body: CancelBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> OrderDetail:
    guest_id = await _guest_id(session, identity)
    try:
        await OrderService(session).cancel_booking(
            guest_id=guest_id, order_no=order_no, reason=body.reason
        )
        await session.commit()
        detail = await _detail(session, guest_id=guest_id, order_no=order_no)
    except DomainError as exc:
        _raise_http(exc)
    await session.commit()
    return detail


@router.post(
    "/{order_no}/invoice",
    response_model=InvoiceSummary,
    status_code=status.HTTP_201_CREATED,
)
async def apply_my_invoice(
    order_no: str,
    body: InvoiceBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
) -> InvoiceSummary:
    """发票申请：同一订单至多一张；重复申请回放既有发票并以 200 区分。"""
    guest_id = await _guest_id(session, identity)
    try:
        replayed = await _existing_invoice(session, guest_id=guest_id, order_no=order_no)
        invoice = await OrderService(session).apply_invoice(
            guest_id=guest_id,
            order_no=order_no,
            title=body.title.strip(),
            tax_no=body.tax_no.strip() if body.tax_no else None,
        )
        await session.commit()
    except DomainError as exc:
        _raise_http(exc)
    if replayed is not None:
        response.status_code = status.HTTP_200_OK
    return _invoice_summary(invoice)


async def _existing_invoice(
    session: AsyncSession, *, guest_id: UUID, order_no: str
) -> Invoice | None:
    """回放判定：只认本人订单（归属复验仍由 apply_invoice 执行处兜底）。"""
    order = await OrderRepository(session).get_by_order_no(order_no)
    if order is None or order.guest_id != guest_id:
        return None
    return await InvoiceRepository(session).get_by_order(order.id)


@router.post("/{order_no}/pay", response_model=PayInitiationResponse)
async def initiate_order_payment(
    order_no: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(require_identity)],
    client: Annotated[WechatClient, Depends(get_wechat_client)],
) -> PayInitiationResponse:
    service = PaymentService(session)
    try:
        initiation = await service.initiate(
            user_id=identity.user_id, order_no=order_no, client=client
        )
    except DomainError as exc:
        _raise_http(exc)
    except WechatUnavailableError as exc:
        logger.warning("微信支付下单不可达：order_no=%s", order_no)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="微信支付服务暂不可用，请稍后重试",
        ) from exc
    except WechatError as exc:
        logger.warning("微信支付下单失败：order_no=%s error=%s", order_no, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="支付发起失败，请稍后重试"
        ) from exc
    await session.commit()
    return PayInitiationResponse(
        order_no=initiation.order_no,
        amount_fen=initiation.amount_fen,
        prepay_id=initiation.prepay_id,
        pay_params=initiation.pay_params,
    )
