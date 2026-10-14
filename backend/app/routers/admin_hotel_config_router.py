"""酒店配置管理：单行配置，GET 读取 / PUT 更新。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.domain import HotelConfig
from app.security import Identity, require_roles

router = APIRouter(prefix="/admin/hotel-config", tags=["admin-hotel-config"])

read_guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(read_guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]


class HotelConfigResponse(BaseModel):
    hotel_name: str
    address: str | None = None
    phone: str | None = None
    brand: str | None = None
    description: str | None = None


class HotelConfigPutBody(BaseModel):
    hotel_name: str = Field(min_length=1, max_length=128)
    address: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    brand: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


async def _get_or_create(session: AsyncSession) -> HotelConfig:
    stmt = select(HotelConfig).where(HotelConfig.id == 1)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = HotelConfig(id=1)
        session.add(row)
        await session.flush()
    return row


@router.get("", response_model=HotelConfigResponse)
async def get_hotel_config(
    session: SessionDep,
    _identity: GuardDep,
) -> HotelConfigResponse:
    cfg = await _get_or_create(session)
    return HotelConfigResponse(
        hotel_name=cfg.hotel_name,
        address=cfg.address,
        phone=cfg.phone,
        brand=cfg.brand,
        description=cfg.description,
    )


@router.put("", response_model=HotelConfigResponse)
async def update_hotel_config(
    body: HotelConfigPutBody,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> HotelConfigResponse:
    cfg = await _get_or_create(session)
    cfg.hotel_name = body.hotel_name
    cfg.address = body.address
    cfg.phone = body.phone
    cfg.brand = body.brand
    cfg.description = body.description
    await session.commit()
    await session.refresh(cfg)
    return HotelConfigResponse(
        hotel_name=cfg.hotel_name,
        address=cfg.address,
        phone=cfg.phone,
        brand=cfg.brand,
        description=cfg.description,
    )
