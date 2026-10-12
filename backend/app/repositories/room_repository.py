"""房型与房间仓储：基础数据读写原语；房态取值见术语表 §11。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Room, RoomType
from app.repositories.support import affected_rows


class RoomTypeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        code: str,
        name: str,
        base_price_fen: int,
        capacity: int,
        bed_type: str,
        description: str | None = None,
        amenities: Sequence[str] | None = None,
        active: bool = True,
    ) -> RoomType:
        room_type = RoomType(
            code=code,
            name=name,
            base_price_fen=base_price_fen,
            capacity=capacity,
            bed_type=bed_type,
            description=description,
            amenities=list(amenities) if amenities is not None else None,
            active=active,
        )
        self._session.add(room_type)
        await self._session.flush()
        return room_type

    async def get_by_id(self, room_type_id: UUID) -> RoomType | None:
        return await self._session.get(RoomType, room_type_id, populate_existing=True)

    async def get_by_code(self, code: str) -> RoomType | None:
        stmt = select(RoomType).where(RoomType.code == code)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(
        self, *, include_inactive: bool = False, search: str | None = None
    ) -> list[RoomType]:
        stmt = select(RoomType)
        if not include_inactive:
            stmt = stmt.where(RoomType.active.is_(True))
        if search:
            like = f"%{search}%"
            stmt = stmt.where(
                    or_(RoomType.code.ilike(like), RoomType.name.ilike(like))
                )
        stmt = stmt.order_by(RoomType.code)
        return list((await self._session.execute(stmt)).scalars())

    async def update_fields(self, room_type_id: UUID, values: Mapping[str, Any]) -> bool:
        result = await self._session.execute(
            update(RoomType)
            .where(RoomType.id == room_type_id)
            .values(**dict(values))
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1


class RoomRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        room_no: str,
        room_type_id: UUID,
        floor: int,
        status: str = "available",
        lock_device_no: str | None = None,
    ) -> Room:
        room = Room(
            room_no=room_no,
            room_type_id=room_type_id,
            floor=floor,
            status=status,
            lock_device_no=lock_device_no,
        )
        self._session.add(room)
        await self._session.flush()
        return room

    async def get_by_id(self, room_id: UUID) -> Room | None:
        return await self._session.get(Room, room_id, populate_existing=True)

    async def get_by_room_no(self, room_no: str) -> Room | None:
        stmt = select(Room).where(Room.room_no == room_no)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(
        self, *, room_type_id: UUID | None = None, status: str | None = None, search: str | None = None
    ) -> list[Room]:
        stmt = select(Room)
        if room_type_id is not None:
            stmt = stmt.where(Room.room_type_id == room_type_id)
        if status is not None:
            stmt = stmt.where(Room.status == status)
        if search:
            stmt = stmt.where(Room.room_no.ilike(f"%{search}%"))
        stmt = stmt.order_by(Room.room_no)
        return list((await self._session.execute(stmt)).scalars())

    async def count_by_type(self, room_type_id: UUID) -> int:
        """房型库存：该房型的物理房间数（日期窗占用数与它比较得出可售结论）。"""
        stmt = select(func.count()).select_from(Room).where(Room.room_type_id == room_type_id)
        return int((await self._session.execute(stmt)).scalar_one())

    async def update_fields(self, room_id: UUID, values: Mapping[str, Any]) -> bool:
        result = await self._session.execute(
            update(Room)
            .where(Room.id == room_id)
            .values(**dict(values))
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
