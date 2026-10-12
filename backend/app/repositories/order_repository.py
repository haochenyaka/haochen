"""订单仓储：状态迁移以守卫式 UPDATE 落库，events 只追加不覆盖。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.states import ORDER_STAY_STATUSES, ORDER_TERMINAL_STATUSES
from app.models.domain import Order, Room
from app.repositories.support import affected_rows


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        order_no: str,
        guest_id: UUID,
        room_type_id: UUID,
        check_in_date: date,
        check_out_date: date,
        nights: int,
        amount_fen: int,
        room_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> Order:
        order = Order(
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
        self._session.add(order)
        await self._session.flush()
        return order

    async def get_by_id(self, order_id: UUID) -> Order | None:
        return await self._session.get(Order, order_id, populate_existing=True)

    async def get_by_id_for_update(self, order_id: UUID) -> Order | None:
        """行锁读取：状态机迁移的串行化入口（锁序 订单 → 其他对象）。"""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_order_no(self, order_no: str) -> Order | None:
        stmt = (
            select(Order)
            .where(Order.order_no == order_no)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> Order | None:
        stmt = select(Order).where(Order.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_active_for_guest(self, guest_id: UUID) -> list[Order]:
        """未终态订单（created → checkout_pending），按创建时间倒序。"""
        stmt = (
            select(Order)
            .where(Order.guest_id == guest_id, Order.status.not_in(ORDER_TERMINAL_STATUSES))
            .order_by(Order.created_at.desc(), Order.id.desc())
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_recent_for_guest(self, guest_id: UUID, *, limit: int = 5) -> list[Order]:
        """客人最近订单（含终态），按创建时间倒序取前 limit 条。"""
        stmt = (
            select(Order)
            .where(Order.guest_id == guest_id)
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def get_active_for_guest_in_room(self, guest_id: UUID, room_no: str) -> Order | None:
        """可见性查询：客人仅在其未终态订单指向该房间时命中（A6 归属校验的落库依据）。"""
        stmt = (
            select(Order)
            .join(Room, Room.id == Order.room_id)
            .where(
                Order.guest_id == guest_id,
                Room.room_no == room_no,
                Order.status.not_in(ORDER_TERMINAL_STATUSES),
            )
            .order_by(Order.created_at.desc(), Order.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def count_overlapping(
        self,
        *,
        room_type_id: UUID,
        check_in_date: date,
        check_out_date: date,
        exclude_order_id: UUID | None = None,
    ) -> int:
        """占用数：未终态订单里与本日期窗 [check_in, check_out) 相交的订单条数。"""
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(
                Order.room_type_id == room_type_id,
                Order.status.not_in(ORDER_TERMINAL_STATUSES),
                Order.check_in_date < check_out_date,
                Order.check_out_date > check_in_date,
            )
        )
        if exclude_order_id is not None:
            stmt = stmt.where(Order.id != exclude_order_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_room_overlaps(
        self,
        *,
        room_id: UUID,
        check_in_date: date,
        check_out_date: date,
        exclude_order_id: UUID | None = None,
    ) -> int:
        """房间分配冲突检测：同一物理房间上与本日期窗相交的未终态订单条数。"""
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(
                Order.room_id == room_id,
                Order.status.not_in(ORDER_TERMINAL_STATUSES),
                Order.check_in_date < check_out_date,
                Order.check_out_date > check_in_date,
            )
        )
        if exclude_order_id is not None:
            stmt = stmt.where(Order.id != exclude_order_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_room_ids_with_open_stay(self, *, on_date: date) -> set[UUID]:
        """住宿存续房间集合：未终态且在 on_date 或之后才结束的订单所占房间。

        覆盖在住、在途（未到店但已支付并持有凭证）与当天退房的订单：这些房间的凭证
        在客人手中，每日轮换必须跳过（退房完成、订单终态后才回到轮换范围）。
        """
        stmt = (
            select(Order.room_id)
            .where(
                Order.room_id.is_not(None),
                Order.status.in_(tuple(ORDER_STAY_STATUSES)),
                Order.check_out_date >= on_date,
            )
            .distinct()
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {room_id for room_id in rows if room_id is not None}

    async def apply_fields(
        self,
        order_id: UUID,
        *,
        from_statuses: Sequence[str],
        values: Mapping[str, Any],
        event: Mapping[str, Any] | None = None,
    ) -> bool:
        """守卫式字段更新（不改状态）：改期/续订等只追加事件的非状态写入。"""
        assignments: dict[str, Any] = dict(values)
        if event is not None:
            assignments["events"] = Order.events.op("||")(literal([dict(event)], JSONB))
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(tuple(from_statuses)))
            .values(**assignments)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def transition(
        self,
        order_id: UUID,
        *,
        from_statuses: Sequence[str],
        to_status: str,
        values: Mapping[str, Any] | None = None,
        event: Mapping[str, Any] | None = None,
    ) -> bool:
        """守卫式迁移：仅当当前状态在 from_statuses 内才更新；未命中返回 False。

        event 经 JSONB 拼接追加到 events 末尾，既有事件不会被覆盖。
        """
        assignments: dict[str, Any] = {"status": to_status}
        if values:
            assignments.update(values)
        if event is not None:
            assignments["events"] = Order.events.op("||")(literal([dict(event)], JSONB))
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(tuple(from_statuses)))
            .values(**assignments)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
