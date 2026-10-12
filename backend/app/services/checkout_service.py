"""退房用例服务：发起退房 → 派 checkout_clean 任务 → 改密验证 → 订单闭环。

不变量：
- 退房只允许 checked_in 状态发起（→ checkout_pending）；
- checkout_clean 任务自动派给保洁团队（assigned_staff_id=null，由调度收敛）；
- 任务经 verified(password_rotated=true) 后，门锁凭证吊销 → 订单 completed；
- 门锁不可达时订单保持 checkout_pending、任务 escalated，不跳过改密。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.lock.client import LockClient
from app.lock.service import LockCredentialService
from app.models.domain import Order
from app.repositories.housekeeping_repository import HousekeepingTaskRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.room_repository import RoomRepository
from app.services.housekeeping_service import HousekeepingService
from app.services.order_service import OrderService


class CheckoutService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrderRepository(session)
        self._rooms = RoomRepository(session)
        self._tasks = HousekeepingTaskRepository(session)
        self._housekeeping = HousekeepingService(session)
        self._lock = LockCredentialService(session)

    async def initiate_checkout(
        self, *, guest_id: UUID, order_no: str
    ) -> Order:
        """客人发起退房：订单 → checkout_pending + 派 checkout_clean 任务。"""
        order = await self._require_owned(guest_id=guest_id, order_no=order_no)
        if order.room_id is None:
            raise ConflictError("订单尚未分配房间，无法退房")
        room = await self._rooms.get_by_id(order.room_id)
        if room is None:
            raise NotFoundError("room", order.room_id)

        order_service = OrderService(self._session)
        order = await order_service.transition(
            order.id, "checkout_pending",
            event={
                "type": "checkout_initiated",
                "guest_id": str(guest_id),
                "room_no": room.room_no,
            },
        )

        await self._housekeeping.create(
            task_type="checkout_clean",
            room_id=room.id,
            room_no=room.room_no,
            order_id=order.id,
        )
        return order

    async def verify_and_complete(
        self, *, task_id: UUID, lock_client: LockClient
    ) -> Order | None:
        """保洁确认改密后完成退房：吊销门锁凭证 → 订单 completed。

        返回更新后的订单，若该任务不关联订单则返回 None。
        """
        task = await self._tasks.get_by_id(task_id)
        if task is None or task.order_id is None:
            return None

        order = await self._orders.get_by_id_for_update(task.order_id)
        if order is None or order.status != "checkout_pending":
            return None

        # 吊销门锁凭证（防旧码复用）
        try:
            await self._lock.revoke(room_no=task.room_no, client=lock_client)
        except NotFoundError:
            # 凭证已不活跃（已被吊销或过期），仍允许完成退房
            pass

        order_service = OrderService(self._session)
        return await order_service.transition(
            order.id, "completed",
            event={
                "type": "checkout_completed",
                "room_no": task.room_no,
                "task_id": str(task_id),
            },
        )

    async def _require_owned(self, *, guest_id: UUID, order_no: str) -> Order:
        normalized = order_no.strip().upper()
        order = await self._orders.get_by_order_no(normalized)
        if order is None:
            raise NotFoundError("order", normalized)
        if order.guest_id != guest_id:
            raise PermissionDeniedError(f"订单不属于当前客人：{normalized}")
        if order.status != "checked_in":
            raise ConflictError(f"订单当前状态不允许退房：{order.status}")
        return order