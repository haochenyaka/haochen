"""MCP 服务器管理接口：CRUD 与连接测试。

权限：admin 可读写，manager 只读。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.mcp.client import McpConfigError
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
    transport: str = "streamable_http"
    config_json: dict[str, object] | None = None
    active: bool = True
    last_status: str | None = None
    last_error: str | None = None
    last_tested_at: str | None = None
    discovered_tools: list[dict[str, object]] | None = None
    created_at: str
    updated_at: str


class CreateMcpServerBody(BaseModel):
    slug: str
    name: str
    server_url: str
    transport: str = "streamable_http"
    description: str | None = None
    config_json: dict[str, object] | None = None
    active: bool = True


class UpdateMcpServerBody(BaseModel):
    name: str | None = None
    description: str | None = None
    server_url: str | None = None
    transport: str | None = None
    config_json: dict[str, object] | None = None
    active: bool | None = None


class McpToolModel(BaseModel):
    name: str
    description: str = ""


class McpToolsModel(BaseModel):
    server_id: int
    slug: str
    tools: list[McpToolModel]


class McpProbeModel(BaseModel):
    ok: bool
    status: str
    latency_ms: int = 0
    error: str | None = None
    tool_count: int = 0


# ── Helpers ──────────────────────────────────────────────────────


def _to_model(dto: McpServerDTO) -> McpServerModel:
    return McpServerModel(
        id=dto.id,
        slug=dto.slug,
        name=dto.name,
        description=dto.description,
        server_url=dto.server_url,
        transport=dto.transport,
        config_json=dto.config_json,
        active=dto.active,
        last_status=dto.last_status,
        last_error=dto.last_error,
        last_tested_at=dto.last_tested_at.isoformat() if dto.last_tested_at else None,
        discovered_tools=dto.discovered_tools,
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
            transport=body.transport,
            description=body.description,
            config_json=body.config_json,
            active=body.active,
        )
    except McpConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
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
        transport=body.transport,
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


@router.post("/{server_id}/test", response_model=McpProbeModel)
async def test_mcp_server(
    server_id: int,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> McpProbeModel:
    """测试连通性：只发现工具（initialize + tools/list），不执行任何工具。"""
    svc = McpServerService(session)
    try:
        result = await svc.test_connection(server_id)
    except McpConfigError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return McpProbeModel(
        ok=result.ok,
        status=result.status,
        latency_ms=result.latency_ms,
        error=result.error,
        tool_count=len(result.tools),
    )


@router.get("/{server_id}/tools", response_model=McpToolsModel)
async def list_mcp_tools(
    server_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> McpToolsModel:
    """列出最近一次探测发现的工具清单。"""
    svc = McpServerService(session)
    server = await svc.get_server(server_id)
    if server is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MCP 服务器不存在")
    tools = [
        McpToolModel(name=str(t.get("name", "")), description=str(t.get("description", "")))
        for t in (server.discovered_tools or [])
    ]
    return McpToolsModel(server_id=server_id, slug=server.slug, tools=tools)
