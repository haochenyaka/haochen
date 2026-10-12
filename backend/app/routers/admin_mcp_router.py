"""MCP 服务器管理接口：CRUD 与连接测试。

权限：admin 可读写，manager 只读。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.mcp_server_service import McpServerDTO, McpServerService

router = APIRouter(prefix="/admin/mcp-servers", tags=["admin-mcp-servers"])

guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]


# ── Request/Response models ──────────────────────────────────────


class McpServerModel(BaseModel):
    id: int
    slug: str
    name: str
    description: str | None = None
    server_url: str
    config_json: dict[str, object] | None = None
    active: bool = True
    created_at: str
    updated_at: str


class CreateMcpServerBody(BaseModel):
    slug: str
    name: str
    server_url: str
    description: str | None = None
    config_json: dict[str, object] | None = None
    active: bool = True


class UpdateMcpServerBody(BaseModel):
    name: str | None = None
    description: str | None = None
    server_url: str | None = None
    config_json: dict[str, object] | None = None
    active: bool | None = None


# ── Helpers ──────────────────────────────────────────────────────


def _to_model(dto: McpServerDTO) -> McpServerModel:
    return McpServerModel(
        id=dto.id,
        slug=dto.slug,
        name=dto.name,
        description=dto.description,
        server_url=dto.server_url,
        config_json=dto.config_json,
        active=dto.active,
        created_at=dto.created_at.isoformat(),
        updated_at=dto.updated_at.isoformat(),
    )


# ── Routes ───────────────────────────────────────────────────────


@router.get("", response_model=list[McpServerModel])
async def list_mcp_servers(
    session: SessionDep,
    _identity: GuardDep,
    active_only: bool = False,
) -> list[McpServerModel]:
    svc = McpServerService(session)
    servers = await svc.list_servers(active_only=active_only)
    return [_to_model(s) for s in servers]


@router.get("/{server_id}", response_model=McpServerModel)
async def get_mcp_server(
    server_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> McpServerModel:
    svc = McpServerService(session)
    server = await svc.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP 服务器不存在")
    return _to_model(server)


@router.post("", response_model=McpServerModel, status_code=status.HTTP_201_CREATED)
async def create_mcp_server(
    body: CreateMcpServerBody,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> McpServerModel:
    svc = McpServerService(session)
    try:
        server = await svc.create_server(
            slug=body.slug,
            name=body.name,
            server_url=body.server_url,
            description=body.description,
            config_json=body.config_json,
            active=body.active,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _to_model(server)


@router.put("/{server_id}", response_model=McpServerModel)
async def update_mcp_server(
    server_id: int,
    body: UpdateMcpServerBody,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> McpServerModel:
    svc = McpServerService(session)
    server = await svc.update_server(
        server_id,
        name=body.name,
        description=body.description,
        server_url=body.server_url,
        config_json=body.config_json,
        active=body.active,
    )
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP 服务器不存在")
    return _to_model(server)


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(
    server_id: int,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> None:
    svc = McpServerService(session)
    deleted = await svc.delete_server(server_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP 服务器不存在")