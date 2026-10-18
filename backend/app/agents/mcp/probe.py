"""MCP 连通性探测：只发现工具，不执行副作用。

`probe_server` 连接远程 MCP server，调用 `initialize` + `tools/list`（由
`langchain_mcp_adapters` 的 `get_tools` 完成），返回结构化结论。**绝不 `tools/call`**。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.agents.mcp.client import McpConfigError, discover_tools, validate_transport

logger = logging.getLogger(__name__)

PROBE_OK = "ok"
PROBE_FAILED = "failed"


@dataclass
class ProbeResult:
    ok: bool
    status: str
    tools: list[dict[str, object]] = field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0


async def probe_server(
    *,
    slug: str,
    server_url: str,
    transport: str,
    config: dict[str, object] | None = None,
) -> ProbeResult:
    """探测一次连通性，返回工具清单与耗时。

    失败不抛出：以 `ProbeResult(ok=False)` 返回，错误文案留给管理端展示。
    """
    started = time.monotonic()
    try:
        validate_transport(transport)
        tools = await discover_tools(
            slug=slug, server_url=server_url, transport=transport, config=config
        )
        latency = int((time.monotonic() - started) * 1000)
        return ProbeResult(ok=True, status=PROBE_OK, tools=tools, latency_ms=latency)
    except McpConfigError as exc:
        latency = int((time.monotonic() - started) * 1000)
        logger.warning("MCP 探测配置非法：slug=%s error=%s", slug, exc)
        return ProbeResult(ok=False, status=PROBE_FAILED, error=str(exc), latency_ms=latency)
    except Exception as exc:  # noqa: BLE001 — 探测失败一律收敛为结论，不向上抛
        latency = int((time.monotonic() - started) * 1000)
        logger.warning("MCP 探测失败：slug=%s error=%s", slug, exc)
        message = f"{type(exc).__name__}: {exc}"
        return ProbeResult(ok=False, status=PROBE_FAILED, error=message, latency_ms=latency)


__all__: list[str] = ["PROBE_FAILED", "PROBE_OK", "ProbeResult", "probe_server"]
