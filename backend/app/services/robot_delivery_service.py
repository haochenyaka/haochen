"""机器人配送用例服务：下单、状态查询、设备回调。
 
不变量：
- 创建带 idempotency_key 时重复提交回放既有配送，不重复呼叫设备；
- 状态迁移由设备回调驱动（queued→dispatching→delivering→arrived→delivered/failed）；
- 设备不可达时配送保持 queued 待重试。
"""
 
from __future__ import annotations
 
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID
 
from sqlalchemy.ext.asyncio import AsyncSession
 
from app.domain.errors import ConflictError, NotFoundError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.models.domain import RobotDelivery
from app.repositories.robot_delivery_repository import RobotDeliveryRepository
 
ROBOT_DELIVERY_STATUSES: frozenset[str] = frozenset(
    {"queued", "dispatching", "delivering", "arrived", "delivered", "failed"}
)
 
TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"dispatching", "failed"}),
    "dispatching": frozenset({"delivering", "failed"}),
    "delivering": frozenset({"arrived", "failed"}),
    "arrived": frozenset({"delivered", "failed"}),
    "delivered": frozenset(),
    "failed": frozenset(),
}
 
 
class RobotDeliveryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._deliveries = RobotDeliveryRepository(session)
 
    async def create(
        self,
        *,
        guest_id: UUID,
        room_no: str,
        item: Mapping[str, Any],
        order_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> RobotDelivery:
        if idempotency_key is not None:
            replayed = await self._deliveries.get_by_idempotency_key(idempotency_key)
            if replayed is not None:
                return replayed
        return await self._deliveries.create(
            guest_id=guest_id,
            room_no=room_no,
            item=dict(item),
            order_id=order_id,
            idempotency_key=idempotency_key,
        )
 
    async def get_by_id(self, delivery_id: UUID) -> RobotDelivery:
        delivery = await self._deliveries.get_by_id(delivery_id)
        if delivery is None:
            raise NotFoundError("robot_delivery", delivery_id)
        return delivery
 
    async def list_for_guest(self, guest_id: UUID, limit: int = 20) -> list[RobotDelivery]:
        return await self._deliveries.list_for_guest(guest_id, limit=limit)
 
    async def transition(
        self, delivery_id: UUID, to_status: str, *, fail_reason: str | None = None
    ) -> RobotDelivery:
        delivery = await self._deliveries.get_by_id(delivery_id)
        if delivery is None:
            raise NotFoundError("robot_delivery", delivery_id)
        allowed = TRANSITIONS.get(delivery.status, frozenset())
        if to_status not in allowed:
            raise ConflictError(f"配送状态非法迁移：{delivery.status} → {to_status}")
        values: dict[str, Any] = {"status": to_status}
        if to_status in ("delivered", "failed"):
            values["finished_at"] = datetime.now(HOTEL_TIMEZONE)
        if to_status == "failed":
            values["fail_reason"] = fail_reason
        updated = await self._deliveries.update_fields(delivery.id, values=values)
        if not updated:
            raise ConflictError(f"配送状态并发改写：{delivery_id}")
        await self._session.refresh(delivery)
        return delivery