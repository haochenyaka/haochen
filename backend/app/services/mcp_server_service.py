"""MCP 服务器配置服务：管理端 CRUD。

API key/secret 不落库（验收主张 A2），由外部密钥管理或运行时注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import McpServer


@dataclass
class McpServerDTO:
    id: int
    slug: str
    name: str
    description: str | None
    server_url: str
    config_json: dict[str, object] | None
    active: bool
    created_at: datetime
    updated_at: datetime


class McpServerService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_servers(self, *, active_only: bool = False) -> list[McpServerDTO]:
        stmt = select(McpServer).order_by(McpServer.slug)
        if active_only:
            stmt = stmt.where(McpServer.active.is_(True))
        result = await self._session.execute(stmt)
        return [_to_dto(row) for row in result.scalars().all()]

    async def get_server(self, server_id: int) -> McpServerDTO | None:
        stmt = select(McpServer).where(McpServer.id == server_id)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_dto(row) if row else None

    async def get_server_by_slug(self, slug: str) -> McpServerDTO | None:
        stmt = select(McpServer).where(McpServer.slug == slug)
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        return _to_dto(row) if row else None

    async def create_server(
        self,
        *,
        slug: str,
        name: str,
        server_url: str,
        description: str | None = None,
        config_json: dict[str, object] | None = None,
        active: bool = True,
    ) -> McpServerDTO:
        server = McpServer(
            slug=slug,
            name=name,
            server_url=server_url,
            description=description,
            config_json=config_json,
            active=active,
        )
        self._session.add(server)
        await self._session.commit()
        await self._session.refresh(server)
        return _to_dto(server)

    async def update_server(
        self,
        server_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        server_url: str | None = None,
        config_json: dict[str, object] | None = None,
        active: bool | None = None,
    ) -> McpServerDTO | None:
        stmt = select(McpServer).where(McpServer.id == server_id)
        result = await self._session.execute(stmt)
        server = result.scalar_one_or_none()
        if server is None:
            return None
        if name is not None:
            server.name = name
        if description is not None:
            server.description = description
        if server_url is not None:
            server.server_url = server_url
        if config_json is not None:
            server.config_json = config_json
        if active is not None:
            server.active = active
        await self._session.commit()
        await self._session.refresh(server)
        return _to_dto(server)

    async def delete_server(self, server_id: int) -> bool:
        stmt = select(McpServer).where(McpServer.id == server_id)
        result = await self._session.execute(stmt)
        server = result.scalar_one_or_none()
        if server is None:
            return False
        await self._session.delete(server)
        await self._session.commit()
        return True


def _to_dto(row: McpServer) -> McpServerDTO:
    return McpServerDTO(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        server_url=row.server_url,
        config_json=row.config_json,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
