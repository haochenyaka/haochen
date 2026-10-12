"""酒店基础数据管理接口：房型/房间/员工/账号。

权限：读为 admin/manager，写为 admin（术语表 §11；验收主张 A6 的配置修改负例在此取证）。
删除一律以状态位（active/status）表达，保留外键引用的事实行。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.domain import (
    ACCOUNT_STATUSES,
    ROOM_STATUSES,
    STAFF_POSITIONS,
    USER_ROLES,
)
from app.repositories import (
    RoomRepository,
    RoomTypeRepository,
    StaffRepository,
    UserRepository,
)
from app.security import Identity, hash_password, require_roles

router = APIRouter(prefix="/admin", tags=["admin-basic-data"])

read_guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

INVALID_ROLE_DETAIL = "角色取值非法"
INVALID_ROOM_STATUS_DETAIL = "房态取值非法"


def _conflict(message: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=message)


class RoomTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    base_price_fen: int = Field(ge=0)
    capacity: int = Field(ge=1, le=16)
    bed_type: str = Field(min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    amenities: list[str] | None = None
    active: bool = True


class RoomTypePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_price_fen: int | None = Field(default=None, ge=0)
    capacity: int | None = Field(default=None, ge=1, le=16)
    bed_type: str | None = Field(default=None, min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=2000)
    amenities: list[str] | None = None
    active: bool | None = None


class RoomTypeResponse(BaseModel):
    id: UUID
    code: str
    name: str
    base_price_fen: int
    capacity: int
    bed_type: str
    description: str | None
    amenities: list[str] | None
    active: bool


class RoomCreate(BaseModel):
    room_no: str = Field(min_length=1, max_length=32)
    room_type_id: UUID
    floor: int = Field(ge=0, le=200)
    status: str = "available"
    lock_device_no: str | None = Field(default=None, max_length=64)


class RoomPatch(BaseModel):
    room_type_id: UUID | None = None
    floor: int | None = Field(default=None, ge=0, le=200)
    status: str | None = None
    lock_device_no: str | None = Field(default=None, max_length=64)


class RoomResponse(BaseModel):
    id: UUID
    room_no: str
    room_type_id: UUID
    floor: int
    status: str
    lock_device_no: str | None


class StaffCreate(BaseModel):
    staff_no: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    position: str = "housekeeping"
    user_id: UUID | None = None
    status: str = "active"


class StaffPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    position: str | None = None
    user_id: UUID | None = None
    status: str | None = None


class StaffResponse(BaseModel):
    id: UUID
    staff_no: str
    name: str
    position: str
    user_id: UUID | None
    status: str


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: str
    display_name: str | None = Field(default=None, max_length=64)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = None
    status: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)


class UserResponse(BaseModel):
    id: UUID
    username: str
    display_name: str | None
    role: str
    status: str
    last_login_at: datetime | None


def _validate_role(role: str) -> None:
    if role not in USER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=INVALID_ROLE_DETAIL
        )


def _validate_account_status(value: str) -> None:
    if value not in ACCOUNT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="账号状态取值非法"
        )


def _validate_room_status(value: str) -> None:
    if value not in ROOM_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=INVALID_ROOM_STATUS_DETAIL
        )


def _validate_position(value: str) -> None:
    if value not in STAFF_POSITIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="岗位取值非法"
        )


async def _commit(session: AsyncSession, *, conflict_detail: str) -> None:
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise _conflict(conflict_detail) from exc


@router.get("/room-types", response_model=list[RoomTypeResponse])
async def list_room_types(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
    include_inactive: bool = False,
    search: str | None = Query(None, description="按编码或名称搜索"),
) -> list[RoomTypeResponse]:
    rows = await RoomTypeRepository(session).list_all(include_inactive=include_inactive, search=search)
    return [RoomTypeResponse(**vars(row)) for row in rows]


@router.post("/room-types", response_model=RoomTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_room_type(
    body: RoomTypeCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> RoomTypeResponse:
    repo = RoomTypeRepository(session)
    if await repo.get_by_code(body.code) is not None:
        raise _conflict(f"房型编码已存在：{body.code}")
    row = await repo.create(
        code=body.code,
        name=body.name,
        base_price_fen=body.base_price_fen,
        capacity=body.capacity,
        bed_type=body.bed_type,
        description=body.description,
        amenities=body.amenities,
        active=body.active,
    )
    await _commit(session, conflict_detail=f"房型编码已存在：{body.code}")
    return RoomTypeResponse(**vars(row))


@router.patch("/room-types/{room_type_id}", response_model=RoomTypeResponse)
async def patch_room_type(
    room_type_id: UUID,
    body: RoomTypePatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> RoomTypeResponse:
    repo = RoomTypeRepository(session)
    row = await repo.get_by_id(room_type_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="房型不存在")
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if values:
        await repo.update_fields(room_type_id, values)
        await session.commit()
        row = await repo.get_by_id(room_type_id)
    assert row is not None
    return RoomTypeResponse(**vars(row))


@router.get("/rooms", response_model=list[RoomResponse])
async def list_rooms(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
    room_type_id: UUID | None = None,
    status_filter: str | None = None,
    search: str | None = Query(None, description="按房间号搜索"),
) -> list[RoomResponse]:
    if status_filter is not None:
        _validate_room_status(status_filter)
    rows = await RoomRepository(session).list_all(room_type_id=room_type_id, status=status_filter, search=search)
    return [RoomResponse(**vars(row)) for row in rows]


@router.post("/rooms", response_model=RoomResponse, status_code=status.HTTP_201_CREATED)
async def create_room(
    body: RoomCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> RoomResponse:
    _validate_room_status(body.status)
    rooms = RoomRepository(session)
    if await rooms.get_by_room_no(body.room_no) is not None:
        raise _conflict(f"房间号已存在：{body.room_no}")
    if await RoomTypeRepository(session).get_by_id(body.room_type_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="房型不存在")
    row = await rooms.create(
        room_no=body.room_no,
        room_type_id=body.room_type_id,
        floor=body.floor,
        status=body.status,
        lock_device_no=body.lock_device_no,
    )
    await _commit(session, conflict_detail=f"房间号已存在：{body.room_no}")
    return RoomResponse(**vars(row))


@router.patch("/rooms/{room_id}", response_model=RoomResponse)
async def patch_room(
    room_id: UUID,
    body: RoomPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> RoomResponse:
    rooms = RoomRepository(session)
    row = await rooms.get_by_id(room_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="房间不存在")
    values: dict[str, Any] = body.model_dump(exclude_unset=True, exclude_none=True)
    if "status" in values:
        _validate_room_status(str(values["status"]))
    if (
        "room_type_id" in values
        and await RoomTypeRepository(session).get_by_id(UUID(str(values["room_type_id"]))) is None
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="房型不存在")
    if values:
        await rooms.update_fields(room_id, values)
        await session.commit()
        row = await rooms.get_by_id(room_id)
    assert row is not None
    return RoomResponse(**vars(row))


@router.get("/staff", response_model=list[StaffResponse])
async def list_staff(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(read_guard)],
    status_filter: str | None = None,
    search: str | None = Query(None, description="按姓名或工号搜索"),
) -> list[StaffResponse]:
    if status_filter is not None:
        _validate_account_status(status_filter)
    rows = await StaffRepository(session).list_all(status=status_filter, search=search)
    return [StaffResponse(**vars(row)) for row in rows]


@router.post("/staff", response_model=StaffResponse, status_code=status.HTTP_201_CREATED)
async def create_staff(
    body: StaffCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> StaffResponse:
    _validate_position(body.position)
    _validate_account_status(body.status)
    staff = StaffRepository(session)
    if await staff.get_by_staff_no(body.staff_no) is not None:
        raise _conflict(f"工号已存在：{body.staff_no}")
    row = await staff.create(
        staff_no=body.staff_no,
        name=body.name,
        position=body.position,
        user_id=body.user_id,
        status=body.status,
    )
    await _commit(session, conflict_detail=f"工号已存在：{body.staff_no}")
    return StaffResponse(**vars(row))


@router.patch("/staff/{staff_id}", response_model=StaffResponse)
async def patch_staff(
    staff_id: UUID,
    body: StaffPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> StaffResponse:
    staff = StaffRepository(session)
    row = await staff.get_by_id(staff_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="员工不存在")
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if "position" in values:
        _validate_position(str(values["position"]))
    if "status" in values:
        _validate_account_status(str(values["status"]))
    if values:
        await staff.update_fields(staff_id, values)
        await _commit(session, conflict_detail="员工档案更新冲突")
        row = await staff.get_by_id(staff_id)
    assert row is not None
    return StaffResponse(**vars(row))


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
    role: str | None = None,
    search: str | None = Query(None, description="按用户名或显示名搜索"),
) -> list[UserResponse]:
    if role is not None:
        _validate_role(role)
    rows = await UserRepository(session).list_all(role=role, search=search)
    return [UserResponse(**vars(row)) for row in rows]


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    session: Annotated[AsyncSession, Depends(get_session)],
    _identity: Annotated[Identity, Depends(write_guard)],
) -> UserResponse:
    _validate_role(body.role)
    users = UserRepository(session)
    if await users.get_by_username(body.username) is not None:
        raise _conflict(f"用户名已存在：{body.username}")
    created = await users.create_account(
        username=body.username,
        password_hash=hash_password(body.password),
        role=body.role,
        display_name=body.display_name,
    )
    await _commit(session, conflict_detail=f"用户名已存在：{body.username}")
    row = await users.get_by_id(created.id)
    assert row is not None
    return UserResponse(**vars(row))


@router.patch("/users/{user_id}", response_model=UserResponse)
async def patch_user(
    user_id: UUID,
    body: UserPatch,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(write_guard)],
) -> UserResponse:
    users = UserRepository(session)
    row = await users.get_by_id(user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    if "role" in values:
        _validate_role(str(values["role"]))
    if "status" in values:
        _validate_account_status(str(values["status"]))
    if str(values.get("status", "")) == "disabled" and row.id == identity.user_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="不能禁用当前登录账号")
    if "password" in values:
        values["password_hash"] = hash_password(str(values.pop("password")))
    if values:
        await users.update_fields(user_id, values)
        await _commit(session, conflict_detail="账号更新冲突")
        row = await users.get_by_id(user_id)
    assert row is not None
    return UserResponse(**vars(row))
