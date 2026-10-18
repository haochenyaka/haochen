"""MCP 工具的装配期装载：按授权快照把远程 server 的工具注入 Run 级注册表。

设计（PR-M2）：
- 每个 Run 装配时，按 `AgentConfig.enabled_mcp_servers` 读 active 的 `mcp_servers` 行；
- 经 `langchain_mcp_adapters` 取工具对象，统一加前缀 `{slug}__`（防与内置工具重名）；
- 工具元数据标记 `category=mcp`、`dangerous`（默认 True，`config_json.read_only_tools`
  可豁免）、`read_only`；`config_json.disabled_tools` 声明的工具被剔除；
- 声明但探测失败的 server **跳过其工具并告警**，不阻断 Run（D-MCP-3）；
- 工具经**同一个 `build_tool_node`** 包装 → 自动获得审计/打码/幂等/审批/事件投影。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.tools import BaseTool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.mcp.client import load_tools
from app.db import SessionFactory
from app.models.mcp_server import McpServer

logger = logging.getLogger(__name__)

MCP_TOOL_PREFIX_SEP = "__"
MCP_CATEGORY = "mcp"


def _prefixed(slug: str, tool_name: str) -> str:
    return f"{slug}{MCP_TOOL_PREFIX_SEP}{tool_name}"


def _apply_metadata(
    tool: BaseTool,
    *,
    slug: str,
    read_only_tools: set[str],
    disabled_tools: set[str],
) -> BaseTool | None:
    """重命名 + 标记元数据；被禁用的工具返回 None。"""
    original = tool.name
    if original in disabled_tools:
        return None
    tool.name = _prefixed(slug, original)
    metadata: dict[str, Any] = dict(getattr(tool, "metadata", None) or {})
    metadata.update(
        {
            "category": MCP_CATEGORY,
            "display_name": f"{slug} · {original}",
            # MCP 工具是外部不可信边界：默认危险（走审批），除非显式声明只读
            "dangerous": original not in read_only_tools,
            "read_only": original in read_only_tools,
            "skill": None,
            "credential_delivery": False,
            "mcp_server": slug,
        }
    )
    tool.metadata = metadata
    return tool


async def load_mcp_tools_for_agent(
    slugs: Sequence[str],
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> tuple[BaseTool, ...]:
    """按 server slug 列表装载 MCP 工具；失败/未启用/不可达一律跳过并告警。"""
    if not slugs:
        return ()

    loaded: list[BaseTool] = []
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(McpServer)
                    .where(McpServer.slug.in_(list(slugs)))
                    .where(McpServer.active.is_(True))
                )
            )
            .scalars()
            .all()
        )

    found = {row.slug for row in rows}
    for slug in slugs:
        if slug not in found:
            logger.warning("启用的 MCP server 不存在或已停用，跳过：slug=%s", slug)

    for row in rows:
        cfg = dict(row.config_json or {})
        raw_read_only = cfg.get("read_only_tools")
        raw_disabled = cfg.get("disabled_tools")
        read_only_tools = (
            {str(x) for x in raw_read_only}
            if isinstance(raw_read_only, (list, tuple, set))
            else set()
        )
        disabled_tools = (
            {str(x) for x in raw_disabled}
            if isinstance(raw_disabled, (list, tuple, set))
            else set()
        )
        try:
            tools = await load_tools(
                slug=row.slug,
                server_url=row.server_url,
                transport=row.transport,
                config=row.config_json,
            )
        except Exception as exc:  # noqa: BLE001 — 探测失败不阻断 Run（D-MCP-3）
            logger.warning("MCP server 工具装载失败，跳过：slug=%s error=%s", row.slug, exc)
            continue

        for tool in tools:
            annotated = _apply_metadata(
                tool,
                slug=row.slug,
                read_only_tools=read_only_tools,
                disabled_tools=disabled_tools,
            )
            if annotated is not None:
                loaded.append(annotated)
    return tuple(loaded)


__all__ = ["MCP_CATEGORY", "MCP_TOOL_PREFIX_SEP", "load_mcp_tools_for_agent"]
