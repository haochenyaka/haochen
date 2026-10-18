"""MCP 服务器配置服务：管理端 CRUD + 连通性测试。

API key/secret 不落库（验收主张 A2），由外部密钥管理或运行时注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.mcp.client import McpConfigError, validate_transport
from app.agents.mcp.probe import PROBE_FAILED, PROBE_OK, ProbeResult, probe_server
from app.models.mcp_server import McpServer


@dataclass
class McpServerDTO:
    id: int
    slug: str
    name: str
    description: str | None
    server_url: str
    transport: str
    config_json: dict[str, object] | None
    active: bool
    last_status: str | None
    last_error: str | None
    last_tested_at: datetime | None
    discovered_tools: list[dict[str, object]] | None
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
        transport: str = "streamable_http",
        description: str | None = None,
        config_json: dict[str, object] | None = None,
        active: bool = True,
    ) -> McpServerDTO:
        validate_transport(transport)
        server = McpServer(
            slug=slug,
            name=name,
            server_url=server_url,
            transport=transport,
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
        transport: str | None = None,
        config_json: dict[str, object] | None = None,
        active: bool | None = None,
    ) -> McpServerDTO | None:
        if transport is not None:
            validate_transport(transport)
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
        if transport is not None:
            server.transport = transport
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

    async def test_connection(self, server_id: int) -> ProbeResult:
        """对已注册的 MCP server 做一次连通性测试并回写探测结论。

        只发现工具（initialize + tools/list），**不执行任何工具**。server 不存在时抛
        `McpConfigError`（调用方转 404）。
        """
        stmt = select(McpServer).where(McpServer.id == server_id)
        server = (await self._session.execute(stmt)).scalar_one_or_none()
        if server is None:
            raise McpConfigError(f"MCP 服务器不存在：{server_id}")

        result = await probe_server(
            slug=server.slug,
            server_url=server.server_url,
            transport=server.transport,
            config=server.config_json,
        )
        server.last_status = result.status
        server.last_error = result.error
        server.last_tested_at = datetime.now(UTC)
        server.discovered_tools = result.tools or None
        await self._session.commit()
        return result

    async def get_discovered_tools(self, server_id: int) -> list[dict[str, object]] | None:
        server = await self.get_server(server_id)
        if server is None:
            raise McpConfigError(f"MCP 服务器不存在：{server_id}")
        return server.discovered_tools


def _to_dto(row: McpServer) -> McpServerDTO:
    return McpServerDTO(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        server_url=row.server_url,
        transport=row.transport,
        config_json=row.config_json,
        active=row.active,
        last_status=row.last_status,
        last_error=row.last_error,
        last_tested_at=row.last_tested_at,
        discovered_tools=row.discovered_tools,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = ["PROBE_FAILED", "PROBE_OK", "McpServerDTO", "McpServerService"]
