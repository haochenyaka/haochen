"""订单用例服务：状态机迁移 + 预订/改期/续订/取消/发票（执行处复验）。

不变量：
- 订单状态只经本服务迁移；非法迁移与并发改写抛出 ConflictError，数据库状态保持不变；
- 迁移命中时自动写入对应时间戳列（paid_at / checked_in_at / completed_at / cancelled_at）；
- `events` 只追加不覆盖（改期、续订等事件事实）；
- 创建带 idempotency_key 时重复提交返回既有订单，不新建第二张单（列内唯一约束兜底并发）；
- 预订类用例在服务内复验订单归属、房型有效、日期窗与房态库存（容量与在住占用），
  失败关闭；改期/续订不改变订单状态，只追加事件并重算金额。
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.domain.states import (
    ORDER_TIMESTAMP_COLUMNS,
    IllegalTransitionError,
    ensure_order_transition,
)
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.models.domain import Invoice, Order
from app.repositories.invoice_repository import InvoiceRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.room_repository import RoomRepository, RoomTypeRepository

MAX_NIGHTS = 30
"""单笔订单最长入住晚数；超出走线下长租。"""

RESCHEDULABLE_STATUSES: frozenset[str] = frozenset({"created", "pending_payment", "paid"})
RENEWABLE_STATUSES: frozenset[str] = frozenset({"paid", "checked_in"})


def generate_order_no(now: datetime | None = None) -> str:
    """订单号：H + 日期 + 6 位随机段（唯一性由列约束兜底）。"""
    moment = now or datetime.now(HOTEL_TIMEZONE)
    return f"H{moment:%Y%m%d}{secrets.token_hex(3).upper()}"


class OrderService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrderRepository(session)
        self._room_types = RoomTypeRepository(session)
        self._rooms = RoomRepository(session)
        self._invoices = InvoiceRepository(session)

    async def create(
        self,
        *,
        order_no: str,
        guest_id: UUID,
        room_type_id: UUID,
        check_in_date: date,
        check_out_date: date,
        amount_fen: int,
        room_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        if check_out_date <= check_in_date:
            raise ValidationFailedError("退房日期必须晚于入住日期")
        if amount_fen < 0:
            raise ValidationFailedError("订单金额不能为负")
        nights = (check_out_date - check_in_date).days
        if idempotency_key is not None:
            replayed = await self._orders.get_by_idempotency_key(idempotency_key)
            if replayed is not None:
                return replayed
        return await self._orders.create(
            order_no=order_no,
            guest_id=guest_id,
            room_type_id=room_type_id,
            room_id=room_id,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            nights=nights,
            amount_fen=amount_fen,
            idempotency_key=idempotency_key,
        )

    async def create_booking(
        self,
        *,
        guest_id: UUID,
        room_type_code: str,
        check_in_date: date,
        check_out_date: date,
        guest_count: int = 1,
        idempotency_key: str | None = None,
    ) -> Order:
        """预订用例：执行处复验房型有效、容量、日期窗与房态库存，金额取房型基准价。"""
        if idempotency_key is not None:
            replayed = await self._orders.get_by_idempotency_key(idempotency_key)
            if replayed is not None:
                return replayed
        room_type = await self._room_types.get_by_code(room_type_code.strip().upper())
        if room_type is None or not room_type.active:
            raise ValidationFailedError(f"房型不存在或不可预订：{room_type_code}")
        if guest_count < 1 or guest_count > room_type.capacity:
            raise ValidationFailedError(f"该房型每间最多入住 {room_type.capacity} 人")
        ensure_bookable_window(check_in_date, check_out_date)
        await self._ensure_availability(room_type.id, check_in_date, check_out_date)
        return await self.create(
            order_no=generate_order_no(),
            guest_id=guest_id,
            room_type_id=room_type.id,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            amount_fen=room_type.base_price_fen * (check_out_date - check_in_date).days,
            idempotency_key=idempotency_key,
        )

    async def reschedule_booking(
        self,
        *,
        guest_id: UUID,
        order_no: str,
        new_check_in_date: date,
        new_check_out_date: date,
    ) -> Order:
        order = await self._require_owned(guest_id=guest_id, order_no=order_no)
        if order.status not in RESCHEDULABLE_STATUSES:
            raise ConflictError(
                f"订单当前状态（{order.status}）不支持改期，已入住或退房中的订单请联系前台"
            )
        ensure_bookable_window(new_check_in_date, new_check_out_date)
        await self._ensure_availability(
            order.room_type_id,
            new_check_in_date,
            new_check_out_date,
            exclude_order_id=order.id,
        )
        nightly = await self._nightly_price(order.room_type_id)
        nights = (new_check_out_date - new_check_in_date).days
        event = {
            "type": "rescheduled",
            "from": _date_range(order.check_in_date, order.check_out_date),
            "to": _date_range(new_check_in_date, new_check_out_date),
        }
        return await self._apply_booking_change(
            order,
            values={
                "check_in_date": new_check_in_date,
                "check_out_date": new_check_out_date,
                "nights": nights,
                "amount_fen": nightly * nights,
            },
            event=event,
        )

    async def renew_booking(
        self, *, guest_id: UUID, order_no: str, new_check_out_date: date
    ) -> Order:
        order = await self._require_owned(guest_id=guest_id, order_no=order_no)
        if order.status not in RENEWABLE_STATUSES:
            raise ConflictError(f"订单当前状态（{order.status}）不支持续订，请先完成支付")
        if new_check_out_date <= order.check_out_date:
            raise ValidationFailedError("续订后的退房日期必须晚于当前退房日期")
        nights = (new_check_out_date - order.check_in_date).days
        if nights > MAX_NIGHTS:
            raise ValidationFailedError(f"单笔订单最多 {MAX_NIGHTS} 晚，长租请联系前台")
        await self._ensure_availability(
            order.room_type_id,
            order.check_out_date,
            new_check_out_date,
            exclude_order_id=order.id,
        )
        nightly = await self._nightly_price(order.room_type_id)
        event = {
            "type": "renewed",
            "from": {"check_out_date": order.check_out_date.isoformat()},
            "to": {"check_out_date": new_check_out_date.isoformat()},
        }
        return await self._apply_booking_change(
            order,
            values={
                "check_out_date": new_check_out_date,
                "nights": nights,
                "amount_fen": nightly * nights,
            },
            event=event,
        )

    async def cancel_booking(
        self, *, guest_id: UUID, order_no: str, reason: str | None = None
    ) -> Order:
        order = await self._require_owned(guest_id=guest_id, order_no=order_no)
        event: dict[str, Any] = {"type": "cancelled"}
        if reason:
            event["reason"] = reason
        return await self.transition(order.id, "cancelled", reason=reason, event=event)

    async def apply_invoice(
        self,
        *,
        guest_id: UUID,
        order_no: str,
        title: str,
        tax_no: str | None = None,
    ) -> Invoice:
        """发票申请：完成支付的订单至多一张发票（重复申请回放既有发票）。"""
        order = await self._require_owned(guest_id=guest_id, order_no=order_no)
        if order.paid_at is None:
            raise ConflictError("订单完成支付后才能申请发票")
        existing = await self._invoices.get_by_order(order.id)
        if existing is not None:
            return existing
        amount = order.paid_amount_fen if order.paid_amount_fen is not None else order.amount_fen
        try:
            async with self._session.begin_nested():
                return await self._invoices.create(
                    order_id=order.id,
                    guest_id=guest_id,
                    title=title,
                    tax_no=tax_no,
                    amount_fen=amount,
                    idempotency_key=f"invoice:{order.id}",
                )
        except IntegrityError:
            concurrent = await self._invoices.get_by_order(order.id)
            if concurrent is None:
                raise ConflictError("发票并发创建冲突，请稍后重试") from None
            return concurrent

    async def transition(
        self,
        order_id: UUID,
        to_status: str,
        *,
        reason: str | None = None,
        extra: Mapping[str, Any] | None = None,
        event: Mapping[str, Any] | None = None,
    ) -> Order:
        order = await self._orders.get_by_id_for_update(order_id)
        if order is None:
            raise NotFoundError("order", order_id)
        try:
            ensure_order_transition(order.status, to_status)
        except IllegalTransitionError as exc:
            raise ConflictError(str(exc)) from exc

        values: dict[str, Any] = dict(extra or {})
        timestamp_column = ORDER_TIMESTAMP_COLUMNS.get(to_status)
        if timestamp_column is not None:
            values[timestamp_column] = func.now()
        if to_status == "cancelled" and reason:
            values["cancel_reason"] = reason

        updated = await self._orders.transition(
            order.id,
            from_statuses=(order.status,),
            to_status=to_status,
            values=values,
            event=event,
        )
        if not updated:
            raise ConflictError(f"订单状态并发改写，迁移未生效：{to_status}")
        await self._session.refresh(order)
        return order

    async def _require_owned(self, *, guest_id: UUID, order_no: str) -> Order:
        normalized = order_no.strip().upper()
        order = await self._orders.get_by_order_no(normalized)
        if order is None:
            raise NotFoundError("order", normalized)
        if order.guest_id != guest_id:
            raise PermissionDeniedError(f"订单不属于当前客人：{normalized}")
        return order

    async def _nightly_price(self, room_type_id: UUID) -> int:
        room_type = await self._room_types.get_by_id(room_type_id)
        if room_type is None:
            raise NotFoundError("room_type", room_type_id)
        return room_type.base_price_fen

    async def _ensure_availability(
        self,
        room_type_id: UUID,
        check_in_date: date,
        check_out_date: date,
        *,
        exclude_order_id: UUID | None = None,
    ) -> None:
        inventory = await self._rooms.count_by_type(room_type_id)
        if inventory <= 0:
            raise ConflictError("该房型暂无可售房间，请选择其他房型")
        occupied = await self._orders.count_overlapping(
            room_type_id=room_type_id,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            exclude_order_id=exclude_order_id,
        )
        if occupied >= inventory:
            raise ConflictError("该房型在所选日期已订满，可更换日期或选择其他房型")

    async def _apply_booking_change(
        self,
        order: Order,
        *,
        values: Mapping[str, Any],
        event: Mapping[str, Any],
    ) -> Order:
        updated = await self._orders.apply_fields(
            order.id,
            from_statuses=(order.status,),
            values=values,
            event=event,
        )
        if not updated:
            raise ConflictError("订单状态并发改写，变更未生效，请稍后重试")
        await self._session.refresh(order)
        return order


def ensure_bookable_window(check_in_date: date, check_out_date: date) -> None:
    """可预订日期窗（读侧房态查询与写侧预订/改期共用同一口径）：不早于今天、1–MAX_NIGHTS 晚。"""
    today = datetime.now(HOTEL_TIMEZONE).date()
    if check_in_date < today:
        raise ValidationFailedError("入住日期不能早于今天")
    nights = (check_out_date - check_in_date).days
    if nights < 1:
        raise ValidationFailedError("退房日期必须晚于入住日期")
    if nights > MAX_NIGHTS:
        raise ValidationFailedError(f"单笔订单最多 {MAX_NIGHTS} 晚，长租请联系前台")


def _date_range(check_in_date: date, check_out_date: date) -> dict[str, str]:
    return {
        "check_in_date": check_in_date.isoformat(),
        "check_out_date": check_out_date.isoformat(),
    }
