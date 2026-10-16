"""记忆管理接口：记忆列表/详情/敏感标记/软删。

权限：admin/manager 可读，admin 可写（敏感标记、删除、恢复）。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.admin_memory_service import AdminMemoryService

router = APIRouter(prefix="/admin/memories", tags=["admin-memories"])

guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]


# ── Models ───────────────────────────────────────────────────────


class MemoryModel(BaseModel):
    id: str
    guest_id: str
    guest_nickname: str | None = None
    guest_user_id: str | None = None
    category: str
    content: str
    sensitive: bool = False
    source: str | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None


class MemoryListResponse(BaseModel):
    items: list[MemoryModel]
    total: int
    categories: list[str] = []


class PatchMemoryBody(BaseModel):
    sensitive: bool | None = None
    category: str | None = None


# ── Routes ───────────────────────────────────────────────────────


@router.get("", response_model=MemoryListResponse)
async def list_memories(
    session: SessionDep,
    _identity: GuardDep,
    guest_search: str | None = Query(None, description="搜索客人昵称"),
    category: str | None = Query(None, description="按分类筛选"),
    sensitive: bool | None = Query(None, description="仅敏感/仅非敏感"),
    include_deleted: bool = Query(False, description="包含已软删"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
) -> MemoryListResponse:
    svc = AdminMemoryService(session)
    items, total = await svc.list_memories(
        guest_search=guest_search,
        category=category,
        sensitive=sensitive,
        include_deleted=include_deleted,
        offset=offset,
        limit=limit,
    )
    categories = await svc.list_categories()
    return MemoryListResponse(
        items=[
            MemoryModel(
                id=item.id,
                guest_id=item.guest_id,
                guest_nickname=item.guest_nickname,
                guest_user_id=item.guest_user_id,
                category=item.category,
                content=item.content,
                sensitive=item.sensitive,
                source=item.source,
                created_at=item.created_at.isoformat(),
                updated_at=item.updated_at.isoformat(),
                deleted_at=item.deleted_at.isoformat() if item.deleted_at else None,
            )
            for item in items
        ],
        total=total,
        categories=categories,
    )


@router.get("/categories", response_model=list[str])
async def list_categories(
    session: SessionDep,
    _identity: GuardDep,
) -> list[str]:
    svc = AdminMemoryService(session)
    return await svc.list_categories()


@router.get("/{memory_id}", response_model=MemoryModel)
async def get_memory(
    memory_id: UUID,
    session: SessionDep,
    _identity: GuardDep,
) -> MemoryModel:
    svc = AdminMemoryService(session)
    item = await svc.get_memory(memory_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    return MemoryModel(
        id=item.id,
        guest_id=item.guest_id,
        guest_nickname=item.guest_nickname,
        guest_user_id=item.guest_user_id,
        category=item.category,
        content=item.content,
        sensitive=item.sensitive,
        source=item.source,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
        deleted_at=item.deleted_at.isoformat() if item.deleted_at else None,
    )


@router.patch("/{memory_id}", response_model=MemoryModel)
async def patch_memory(
    memory_id: UUID,
    body: PatchMemoryBody,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> MemoryModel:
    svc = AdminMemoryService(session)
    item = await svc.update_memory(memory_id, sensitive=body.sensitive, category=body.category)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    return MemoryModel(
        id=item.id,
        guest_id=item.guest_id,
        guest_nickname=item.guest_nickname,
        guest_user_id=item.guest_user_id,
        category=item.category,
        content=item.content,
        sensitive=item.sensitive,
        source=item.source,
        created_at=item.created_at.isoformat(),
        updated_at=item.updated_at.isoformat(),
        deleted_at=item.deleted_at.isoformat() if item.deleted_at else None,
    )


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def soft_delete_memory(
    memory_id: UUID,
    session: SessionDep,
    _identity: WriteGuardDep,
    permanent: bool = Query(False, description="物理删除（不可恢复）"),
) -> None:
    svc = AdminMemoryService(session)
    if permanent:
        ok = await svc.delete_permanently(memory_id)
    else:
        ok = await svc.soft_delete(memory_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")


@router.post("/{memory_id}/restore", response_model=MemoryModel)
async def restore_memory(
    memory_id: UUID,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> MemoryModel:
    svc = AdminMemoryService(session)
    ok = await svc.restore(memory_id)
    if not ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    return await get_memory(memory_id, session, _identity)
