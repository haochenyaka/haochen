"""MCP 集成：远程客户端、工具发现与连通性探测（管理端「测试连接」的落地）。"""

from __future__ import annotations

from app.agents.mcp.client import (
    ALLOWED_TRANSPORTS,
    McpConfigError,
    build_connection,
    config_hash,
    discover_tools,
    load_tools,
    tool_cache,
    validate_transport,
)
from app.agents.mcp.probe import PROBE_FAILED, PROBE_OK, ProbeResult, probe_server

__all__ = [
    "ALLOWED_TRANSPORTS",
    "PROBE_FAILED",
    "PROBE_OK",
    "McpConfigError",
    "ProbeResult",
    "build_connection",
    "config_hash",
    "discover_tools",
    "load_tools",
    "probe_server",
    "tool_cache",
    "validate_transport",
]
