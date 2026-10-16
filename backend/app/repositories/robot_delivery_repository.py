"""机器人配送仓储。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import RobotDelivery
from app.repositories.support import affected_rows


class RobotDeliveryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        guest_id: UUID,
        room_no: str,
        item: dict[str, Any],
        order_id: UUID | None = None,
        idempotency_key: str | None = None,
    ) -> RobotDelivery:
        delivery = RobotDelivery(
            order_id=order_id,
            guest_id=guest_id,
            room_no=room_no,
            item=item,
            idempotency_key=idempotency_key,
        )
        self._session.add(delivery)
        await self._session.flush()
        return delivery

    async def get_by_id(self, delivery_id: UUID) -> RobotDelivery | None:
        return await self._session.get(RobotDelivery, delivery_id, populate_existing=True)

    async def get_by_idempotency_key(self, idempotency_key: str) -> RobotDelivery | None:
        stmt = select(RobotDelivery).where(RobotDelivery.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_guest(self, guest_id: UUID, limit: int = 20) -> list[RobotDelivery]:
        stmt = (
            select(RobotDelivery)
            .where(RobotDelivery.guest_id == guest_id)
            .order_by(RobotDelivery.created_at)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def update_fields(self, delivery_id: UUID, values: Mapping[str, Any]) -> bool:
        result = await self._session.execute(
            update(RobotDelivery)
            .where(RobotDelivery.id == delivery_id)
            .values(**dict(values))
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
