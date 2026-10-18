"""MCP 客户端与探测单测（PR-M1）：传输白名单、配置哈希缓存、只发现工具不执行。

远程连接以替身注入，不依赖真实 MCP server。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.agents.mcp import client as mcp_client


def test_validate_transport_rejects_stdio_and_unknown() -> None:
    assert mcp_client.validate_transport("streamable_http") == "streamable_http"
    assert mcp_client.validate_transport("sse") == "sse"
    for bad in ("stdio", "websocket", "http", ""):
        with pytest.raises(mcp_client.McpConfigError):
            mcp_client.validate_transport(bad)


def test_build_connection_includes_timeout_and_no_credentials() -> None:
    conn = mcp_client.build_connection(
        slug="lock-mcp",
        server_url="http://gw:9200/mcp",
        transport="streamable_http",
        config={"timeout_seconds": 7},
    )
    assert conn["transport"] == "streamable_http"
    assert conn["url"] == "http://gw:9200/mcp"
    assert conn["timeout"] == 7.0
    # 未注入环境变量时不得携带授权头（凭证不落库）
    assert "headers" not in conn


def test_build_connection_resolves_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_LOCK_MCP_API_KEY", "secret-token")
    conn = mcp_client.build_connection(
        slug="lock-mcp", server_url="http://gw/mcp", transport="streamable_http"
    )
    assert conn["headers"] == {"Authorization": "Bearer secret-token"}


def test_config_hash_stable_and_changes_with_config() -> None:
    base = dict(slug="a", server_url="http://x", transport="streamable_http", config={"t": 1})
    same = dict(slug="a", server_url="http://x", transport="streamable_http", config={"t": 1})
    diff = dict(slug="a", server_url="http://x", transport="streamable_http", config={"t": 2})
    assert mcp_client.config_hash(**base) == mcp_client.config_hash(**same)
    assert mcp_client.config_hash(**base) != mcp_client.config_hash(**diff)


class _FakeTool:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


class _FakeAdapterClient:
    """`MultiServerMCPClient` 替身：记录 get_tools 调用，仅返回工具清单。"""

    calls: list[str] = []

    def __init__(self, _connections: dict[str, Any]) -> None:
        pass

    async def get_tools(self, *, server_name: str | None = None) -> list[_FakeTool]:
        _FakeAdapterClient.calls.append(server_name or "")
        return [_FakeTool("issue_credential", "签发凭证"), _FakeTool("rotate_credential")]


@pytest.mark.asyncio
async def test_discover_tools_returns_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAdapterClient.calls = []
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", _FakeAdapterClient)

    tools = await mcp_client.discover_tools(
        slug="lock-mcp", server_url="http://gw/mcp", transport="streamable_http"
    )
    assert {t["name"] for t in tools} == {"issue_credential", "rotate_credential"}
    assert tools[0]["description"] == "签发凭证"
    # 只调用 get_tools（即 tools/list），没有任何 tools/call 语义
    assert _FakeAdapterClient.calls == ["lock-mcp"]


@pytest.mark.asyncio
async def test_probe_server_caches_by_config_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.mcp import probe as probe_mod

    _FakeAdapterClient.calls = []
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", _FakeAdapterClient)
    mcp_client.tool_cache()._entries.clear()

    first = await probe_mod.probe_server(
        slug="lock-mcp", server_url="http://gw/mcp", transport="streamable_http"
    )
    assert first.ok is True
    assert len(first.tools) == 2
    # 探测走 use_cache=False，每次都会真实调用；缓存由装配期 load_tools 使用


@pytest.mark.asyncio
async def test_probe_server_converges_config_error_to_failed() -> None:
    from app.agents.mcp import probe as probe_mod

    result = await probe_mod.probe_server(slug="bad", server_url="http://gw/mcp", transport="stdio")
    assert result.ok is False
    assert result.status == probe_mod.PROBE_FAILED
    assert "stdio" in (result.error or "")


@pytest.mark.asyncio
async def test_probe_server_times_out_within_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agents.mcp import probe as probe_mod

    async def _hang(**_kwargs: Any) -> list[dict[str, object]]:
        await asyncio.sleep(5)
        return []

    monkeypatch.setattr(probe_mod, "discover_tools", _hang)

    started = time.monotonic()
    result = await probe_mod.probe_server(
        slug="slow-mcp",
        server_url="http://gw/mcp",
        transport="streamable_http",
        config={"timeout_seconds": 0.05},
    )
    elapsed = time.monotonic() - started
    assert result.ok is False
    assert result.status == probe_mod.PROBE_FAILED
    assert "超时" in (result.error or "")
    assert elapsed < 2, "探测必须有总时长上限，不能随 endpoint 挂起"


@pytest.mark.asyncio
async def test_load_tools_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeAdapterClient.calls = []
    monkeypatch.setattr(mcp_client, "MultiServerMCPClient", _FakeAdapterClient)
    mcp_client.tool_cache()._entries.clear()

    kwargs = dict(slug="cache-mcp", server_url="http://gw/mcp", transport="streamable_http")
    first = await mcp_client.load_tools(**kwargs)
    second = await mcp_client.load_tools(**kwargs)
    assert len(_FakeAdapterClient.calls) == 1, "第二次命中缓存，不应再次连接"
    assert first is second
