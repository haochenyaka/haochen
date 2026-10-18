"""MCP 客户端与工具发现：远程 streamable_http / sse，凭证不落库。

边界（`docs/mechanism-baseline.md §2.7` 的目标态在此落地）：
- 只接受远程传输 `streamable_http` / `sse`；`stdio` 仅限代码维护的系统内置项，拒绝来自
  管理端配置的 stdio（禁止从 HTTP/DB 拼接命令）；
- 连接测试只做 initialize + tools/list，**绝不 tools/call**（只发现工具，不执行副作用）；
- 工具对象按配置哈希缓存：配置不变复用，变更即失效；
- 凭证（api_key / 授权头）不落库，按命名约定 `MCP_<SLUG_UPPER>_API_KEY` 从环境注入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

ALLOWED_TRANSPORTS = ("streamable_http", "sse")
DEFAULT_TIMEOUT_SECONDS = 10.0


class McpConfigError(ValueError):
    """MCP 配置非法（传输不支持、形状错误）。"""


def credential_env_var(slug: str) -> str:
    """按命名约定解析该 server 的凭证环境变量名。"""
    return f"MCP_{slug.upper().replace('-', '_')}_API_KEY"


def _resolve_headers(slug: str, config: Mapping[str, Any]) -> dict[str, str] | None:
    """从环境变量解析授权头：`MCP_<SLUG>_API_KEY` → Authorization: Bearer。

    凭证不落库、不来自 HTTP 请求体，只在进程环境里注入。
    """
    api_key = os.environ.get(credential_env_var(slug))
    if not api_key:
        return None
    return {"Authorization": f"Bearer {api_key}"}


def _timeout_seconds(config: Mapping[str, Any]) -> float:
    raw = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def validate_transport(transport: str) -> str:
    if transport not in ALLOWED_TRANSPORTS:
        raise McpConfigError(
            f"不支持的传输：{transport}；仅允许 {', '.join(ALLOWED_TRANSPORTS)}"
            "（stdio 仅限代码维护的系统内置项）"
        )
    return transport


def config_hash(*, slug: str, server_url: str, transport: str, config: Mapping[str, Any]) -> str:
    """配置指纹：slug + url + transport + config_json（排序）的稳定哈希。"""
    payload = json.dumps(
        {
            "slug": slug,
            "server_url": server_url,
            "transport": transport,
            "config": dict(config or {}),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_connection(
    *,
    slug: str,
    server_url: str,
    transport: str,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 `langchain_mcp_adapters` 的连接描述（单 server）。"""
    validate_transport(transport)
    cfg = dict(config or {})
    connection: dict[str, Any] = {
        "transport": transport,
        "url": server_url,
        "timeout": _timeout_seconds(cfg),
    }
    headers = _resolve_headers(slug, cfg)
    if headers:
        connection["headers"] = headers
    return connection


class McpToolCache:
    """工具对象按配置哈希缓存：配置不变复用，变更即失效。"""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, list[BaseTool]]] = {}

    def get(self, key: str) -> list[BaseTool] | None:
        entry = self._entries.get(key)
        return entry[1] if entry else None

    def put(self, key: str, tools: list[BaseTool], *, cache_key: str) -> None:
        self._entries[key] = (cache_key, tools)

    def invalidate(self, key: str) -> None:
        self._entries.pop(key, None)


_tool_cache = McpToolCache()


def tool_cache() -> McpToolCache:
    return _tool_cache


async def load_tools(
    *,
    slug: str,
    server_url: str,
    transport: str,
    config: Mapping[str, Any] | None = None,
    use_cache: bool = True,
) -> list[BaseTool]:
    """连接远程 MCP server 并返回其 LangChain 工具对象（按配置哈希缓存）。"""
    connection = build_connection(
        slug=slug, server_url=server_url, transport=transport, config=config
    )
    key = config_hash(slug=slug, server_url=server_url, transport=transport, config=config or {})
    cache = tool_cache()
    if use_cache:
        cached = cache.get(key)
        if cached is not None:
            return cached

    client = MultiServerMCPClient({slug: connection})  # type: ignore[dict-item]
    tools = await client.get_tools(server_name=slug)
    cache.put(key, tools, cache_key=key)
    return tools


async def discover_tools(
    *,
    slug: str,
    server_url: str,
    transport: str,
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, object]]:
    """连接测试用：只发现工具的元数据（name/description），不执行任何工具。"""
    tools = await load_tools(
        slug=slug, server_url=server_url, transport=transport, config=config, use_cache=False
    )
    return [{"name": tool.name, "description": (tool.description or "").strip()} for tool in tools]
