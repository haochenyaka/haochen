"""C 端房型与房态查询：在售房型、房型详情与指定日期窗的可售情况（只读）。

库存口径与预订用例完全一致：房型物理房间数 vs 同日期窗未终态重叠订单数——查询不预留库存，
下单时由 `OrderService` 执行处复验（读侧数字只是提示，A6：不作为授权与库存边界）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain.errors import DomainError, NotFoundError, ValidationFailedError
from app.models.domain import RoomType
from app.repositories import OrderRepository, RoomRepository, RoomTypeRepository
from app.security import Identity, require_identity
from app.services.order_service import ensure_bookable_window

router = APIRouter(prefix="/guest/room-types", tags=["guest-room-types"])


class Availability(BaseModel):
    """日期窗内的可售情况：`available_rooms` 为 0 即该房型该日期窗已订满。"""

    check_in_date: date
    check_out_date: date
    nights: int
    total_rooms: int
    booked_rooms: int
    available_rooms: int
    amount_fen: int


class RoomTypeOffer(BaseModel):
    code: str
    name: str
    description: str | None
    base_price_fen: int
    capacity: int
    bed_type: str
    amenities: list[str] | None
    availability: Availability | None = None


def _raise_http(exc: DomainError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ValidationFailedError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="房型查询异常")


async def _availability(
    session: AsyncSession,
    room_type: RoomType,
    *,
    check_in_date: date,
    check_out_date: date,
) -> Availability:
    total = await RoomRepository(session).count_by_type(room_type.id)
    booked = await OrderRepository(session).count_overlapping(
        room_type_id=room_type.id,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
    )
    nights = (check_out_date - check_in_date).days
    return Availability(
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        nights=nights,
        total_rooms=total,
        booked_rooms=min(booked, total),
        available_rooms=max(total - booked, 0),
        amount_fen=room_type.base_price_fen * nights,
    )


def _window(check_in_date: date | None, check_out_date: date | None) -> tuple[date, date] | None:
    """日期窗参数：两者必须同时给出，口径与预订一致（否则 422）。"""
    if check_in_date is None and check_out_date is None:
        return None
    if check_in_date is None or check_out_date is None:
        raise ValidationFailedError("入住与退房日期必须同时提供")
    ensure_bookable_window(check_in_date, check_out_date)
    return check_in_date, check_out_date


def _offer(room_type: RoomType, availability: Availability | None) -> RoomTypeOffer:
    return RoomTypeOffer(
        code=room_type.code,
        name=room_type.name,
        description=room_type.description,
        base_price_fen=room_type.base_price_fen,
        capacity=room_type.capacity,
        bed_type=room_type.bed_type,
        amenities=room_type.amenities,
        availability=availability,
    )


@router.get("", response_model=list[RoomTypeOffer])
async def list_room_type_offers(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(require_identity)],
    check_in_date: date | None = None,
    check_out_date: date | None = None,
) -> list[RoomTypeOffer]:
    """在售房型；带日期窗时逐个房型给出可售间数与预估金额。"""
    try:
        window = _window(check_in_date, check_out_date)
        room_types = await RoomTypeRepository(session).list_all()
        offers = [
            _offer(
                room_type,
                await _availability(
                    session,
                    room_type,
                    check_in_date=window[0],
                    check_out_date=window[1],
                )
                if window is not None
                else None,
            )
            for room_type in room_types
        ]
    except DomainError as exc:
        _raise_http(exc)
    return offers


@router.get("/{code}", response_model=RoomTypeOffer)
async def get_room_type_offer(
    code: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(require_identity)],
    check_in_date: date | None = None,
    check_out_date: date | None = None,
) -> RoomTypeOffer:
    """房型详情；不存在或已下架一律 404（不区分二者，避免目录探测）。"""
    try:
        window = _window(check_in_date, check_out_date)
        room_type = await RoomTypeRepository(session).get_by_code(code.strip().upper())
        if room_type is None or not room_type.active:
            raise NotFoundError("room_type", code)
        availability = (
            await _availability(
                session, room_type, check_in_date=window[0], check_out_date=window[1]
            )
            if window is not None
            else None
        )
    except DomainError as exc:
        _raise_http(exc)
    return _offer(room_type, availability)
