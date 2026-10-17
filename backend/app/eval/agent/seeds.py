"""评测前置状态夹具：为有状态用例预置已支付 / 已入住订单。

线上支付与入住由客人在小程序侧自助完成，多轮对话无法把订单推进到"已支付"，因此后置写
路径（续订 / 发票 / 入住信息 / 开门密码）在评测里不可达。夹具按用例 id 派生用户名，先取消
该评测客人此前的未终态订单以释放房态库存（保证重复运行可复现），再真实建单并推进到目标
状态；返回订单号供用例文本以 `{order_no}` 占位引用。

夹具只用于评测；不触碰线上数据（用户名固定为 `eval-*` 前缀空间）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.states import ORDER_TERMINAL_STATUSES, ORDER_TRANSITIONS
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.eval.agent.dataset import SEED_CHECKED_IN, SEED_PAID, SEED_STATES
from app.lock.client import LockClient
from app.lock.service import LockCredentialService
from app.models.domain import Guest, LockCredential, Order, Room, RoomType
from app.models.runtime import User
from app.repositories import (
    GuestRepository,
    RoomRepository,
    RoomTypeRepository,
    UserRepository,
)
from app.repositories.support import affected_rows
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)

PREFERRED_ROOM_TYPE = "TWIN"
"""首选房型代码：库存稳定（≥1 间），避免落到"有房型无房间"的配置。"""

EVAL_USERNAME_PREFIX = "eval-"
"""评测客人用户名前缀：与执行器派生口径一致，也是夹具释放旧单的识别依据。"""

EVAL_RESET_REASON = "eval-seed-reset"
"""夹具清理旧评测订单时写入的原因，便于在审计与事件里识别。"""


class SeedError(RuntimeError):
    """夹具落库失败：房型无库存或订单推进受阻。"""


async def apply_seed(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    username: str,
    seed: str,
) -> str:
    """为评测客人预置目标状态并返回订单号；seed 取值见 `SEED_STATES`。"""
    if seed not in SEED_STATES:
        raise SeedError(f"未知的夹具状态：{seed}")
    async with session_factory() as session:
        user = await UserRepository(session).get_or_create(username=username)
        guest = await GuestRepository(session).get_or_create_by_user(user.id)
        released = await _release_eval_orders(session)
        if released:
            logger.info("评测夹具释放旧评测订单 %d 笔", released)
        room_type = await _bookable_room_type(session)
        today = datetime.now(HOTEL_TIMEZONE).date()
        service = OrderService(session)
        order = await service.create_booking(
            guest_id=guest.id,
            room_type_code=room_type.code,
            check_in_date=today,
            check_out_date=today + timedelta(days=1),
            guest_count=1,
        )
        await service.transition(order.id, "pending_payment")
        paid = await service.transition(
            order.id,
            "paid",
            extra={"paid_amount_fen": order.amount_fen},
            event={"type": "paid", "channel": "eval-seed", "amount_fen": order.amount_fen},
        )
        if seed == SEED_CHECKED_IN:
            checked_in = await service.transition(
                paid.id, "checked_in", event={"type": "checked_in"}
            )
            await _reset_room_credentials(session, room_type_id=room_type.id)
            client = LockClient.from_settings()
            try:
                await LockCredentialService(session).issue_for_order(
                    order_id=checked_in.id, client=client
                )
            finally:
                await client.aclose()
        await session.commit()
        logger.info("评测夹具落库：user=%s seed=%s order_no=%s", username, seed, order.order_no)
        return order.order_no


async def release_eval_orders(*, session_factory: async_sessionmaker[AsyncSession]) -> int:
    """释放全部评测客人名下的未终态订单，供每条用例开跑前调用。

    未挂夹具的用例也会真实建单（订房/取消链路），若不清理，同房型同日期上的历史评测订单
    会逐条累积直到订满。每条用例都是独立线程与独立客人，不依赖其他用例的订单，因此逐条
    释放是安全的；多轮用例只在开跑前释放，轮内状态不受影响。
    """
    async with session_factory() as session:
        released = await _release_eval_orders(session)
        await session.commit()
        return released


async def _release_eval_orders(session: AsyncSession) -> int:
    """取消全部评测客人（用户名 `eval-` 前缀）名下的未终态订单。

    每个用例各自派生用户名，只清自己的旧单会让不同用例的夹具单彼此占满房型库存（同房型
    同期订单数超过房间数即订满），几条用例之后夹具就再也建不出单。评测订单全部落在
    `eval-` 用户名空间，跨用例统一释放即可保证反复运行都可复现。
    """
    stmt = (
        select(Order)
        .join(Guest, Guest.id == Order.guest_id)
        .join(User, User.id == Guest.user_id)
        .where(
            User.username.like(f"{EVAL_USERNAME_PREFIX}%"),
            Order.status.not_in(ORDER_TERMINAL_STATUSES),
        )
    )
    orders = list((await session.execute(stmt)).scalars())
    await _cancel_orders(session, orders)
    return len(orders)


async def _cancel_orders(session: AsyncSession, orders: Sequence[Order]) -> None:
    service = OrderService(session)
    for existing in orders:
        await _retire_order(service, existing)


async def _retire_order(service: OrderService, order: Order) -> None:
    """把旧评测订单沿合法迁移推到终态，释放房态与凭证。

    已入住订单不能直接取消（状态机只允许 checked_in → checkout_pending），需按退房链路
    （checkout_pending → completed）收敛；未支付/已支付订单直接取消即可。
    """
    current = order
    while True:
        allowed = ORDER_TRANSITIONS.get(current.status, frozenset())
        if "cancelled" in allowed:
            await service.transition(
                current.id,
                "cancelled",
                reason=EVAL_RESET_REASON,
                event={"type": "cancelled", "reason": EVAL_RESET_REASON},
            )
            return
        target = next((step for step in ("checkout_pending", "completed") if step in allowed), None)
        if target is None:
            return
        current = await service.transition(
            current.id,
            target,
            event={"type": target, "reason": EVAL_RESET_REASON},
        )


async def _reset_room_credentials(session: AsyncSession, *, room_type_id: UUID) -> int:
    """清掉该房型各房间的历史门锁凭证行，供重新签发。

    开发机上设备侧（模拟器）与本地 `lock_credentials` 会分叉：模拟器被重置后版本计数
    从头开始，本地却仍留有旧版本行，直接签发会撞 `(room_no, version)` 或"同房间至多一条
    active"约束而失败。凭证以设备为准、本地只是副本，故先清掉本房型的历史行，再让签发
    落回一致状态——不清理则密码类用例在开发机上不可复现。
    """
    room_ids = select(Room.id).where(Room.room_type_id == room_type_id)
    result = await session.execute(
        delete(LockCredential).where(LockCredential.room_id.in_(room_ids))
    )
    return affected_rows(result)


async def _bookable_room_type(session: AsyncSession) -> RoomType:
    """取一个确实有房间的有效房型：优先首选房型，否则取库存最多的可售房型。"""
    room_types = await RoomTypeRepository(session).list_all()
    rooms = RoomRepository(session)
    candidates: list[tuple[RoomType, int]] = []
    for room_type in room_types:
        inventory = await rooms.count_by_type(room_type.id)
        if inventory > 0:
            candidates.append((room_type, inventory))
    if not candidates:
        raise SeedError("评测夹具找不到可用房型：请先为 dev 库补齐 rooms")
    for room_type, _ in candidates:
        if room_type.code == PREFERRED_ROOM_TYPE:
            return room_type
    return max(candidates, key=lambda item: item[1])[0]


__all__ = [
    "PREFERRED_ROOM_TYPE",
    "SEED_CHECKED_IN",
    "SEED_PAID",
    "SEED_STATES",
    "SeedError",
    "apply_seed",
    "release_eval_orders",
]
