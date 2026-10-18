"""MCP 运行期装载单测（PR-M2）：前缀、危险标记、单工具禁用、失败跳过。

远程工具以替身注入，不依赖真实 MCP server。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from langchain_core.tools import tool as make_tool

from app.agents.mcp import client as mcp_client
from app.agents.mcp import runtime as mcp_runtime


class _FakeMcpServerRow:
    def __init__(
        self, slug: str, config: dict[str, Any] | None = None, active: bool = True
    ) -> None:
        self.slug = slug
        self.name = slug
        self.server_url = f"http://{slug}/mcp"
        self.transport = "streamable_http"
        self.config_json = config
        self.active = active


class _FakeSession:
    def __init__(self, rows: list[_FakeMcpServerRow]) -> None:
        self._rows = rows

    async def execute(self, _stmt: Any) -> Any:
        rows = self._rows

        class _Res:
            def scalars(self) -> _Res:
                return self

            def all(self) -> list[_FakeMcpServerRow]:
                return rows

        return _Res()

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_e: object) -> None:
        return None


def _factory(rows: list[_FakeMcpServerRow]) -> Any:
    return lambda: _FakeSession(rows)


def _mcp_tool(name: str) -> Any:
    @make_tool
    def _placeholder() -> str:
        """placeholder"""
        return "ok"

    _placeholder.name = name
    return _placeholder


@pytest.mark.asyncio
async def test_load_mcp_tools_prefixes_and_marks_dangerous(monkeypatch: Any) -> None:
    row = _FakeMcpServerRow("lock-mcp")

    async def _fake_load_tools(**_kwargs: Any) -> list[Any]:
        return [_mcp_tool("issue_credential"), _mcp_tool("read_status")]

    monkeypatch.setattr(mcp_runtime, "load_tools", _fake_load_tools)

    tools = await mcp_runtime.load_mcp_tools_for_agent(
        ["lock-mcp"], session_factory=_factory([row])
    )
    names = {t.name for t in tools}
    assert names == {"lock-mcp__issue_credential", "lock-mcp__read_status"}
    for t in tools:
        assert t.metadata["category"] == "mcp"
        assert t.metadata["dangerous"] is True, "MCP 工具默认危险（走审批）"


@pytest.mark.asyncio
async def test_read_only_and_disabled_tools(monkeypatch: Any) -> None:
    row = _FakeMcpServerRow(
        "lock-mcp",
        config={"read_only_tools": ["read_status"], "disabled_tools": ["dangerous_op"]},
    )

    async def _fake_load_tools(**_kwargs: Any) -> list[Any]:
        return [_mcp_tool("read_status"), _mcp_tool("dangerous_op"), _mcp_tool("write_op")]

    monkeypatch.setattr(mcp_runtime, "load_tools", _fake_load_tools)

    tools = await mcp_runtime.load_mcp_tools_for_agent(
        ["lock-mcp"], session_factory=_factory([row])
    )
    by_name = {t.name: t for t in tools}
    assert "lock-mcp__dangerous_op" not in by_name, "禁用工具被剔除"
    assert by_name["lock-mcp__read_status"].metadata["dangerous"] is False
    assert by_name["lock-mcp__read_status"].metadata["read_only"] is True
    assert by_name["lock-mcp__write_op"].metadata["dangerous"] is True


@pytest.mark.asyncio
async def test_skips_unreachable_server(monkeypatch: Any) -> None:
    row = _FakeMcpServerRow("down-mcp")

    async def _boom(**_kwargs: Any) -> list[Any]:
        raise ConnectionError("refused")

    monkeypatch.setattr(mcp_runtime, "load_tools", _boom)

    tools = await mcp_runtime.load_mcp_tools_for_agent(
        ["down-mcp"], session_factory=_factory([row])
    )
    assert tools == (), "不可达 server 跳过其工具，不阻断 Run"


@pytest.mark.asyncio
async def test_skips_hung_server_within_timeout(monkeypatch: Any) -> None:
    slow = _FakeMcpServerRow("slow-mcp", config={"timeout_seconds": 0.05})
    healthy = _FakeMcpServerRow("ok-mcp")

    async def _load(*, slug: str, **_kwargs: Any) -> list[Any]:
        if slug == "slow-mcp":
            await asyncio.sleep(5)
        return [_mcp_tool("x")]

    monkeypatch.setattr(mcp_runtime, "load_tools", _load)

    started = time.monotonic()
    tools = await mcp_runtime.load_mcp_tools_for_agent(
        ["slow-mcp", "ok-mcp"], session_factory=_factory([slow, healthy])
    )
    elapsed = time.monotonic() - started
    assert {t.name for t in tools} == {"ok-mcp__x"}, "超时 server 跳过，其余照常装载"
    assert elapsed < 2, "单个 server 挂起不能拖垮装配"


@pytest.mark.asyncio
async def test_inactive_or_missing_server_skipped(monkeypatch: Any) -> None:
    async def _fake_load_tools(**_kwargs: Any) -> list[Any]:
        return [_mcp_tool("x")]

    monkeypatch.setattr(mcp_runtime, "load_tools", _fake_load_tools)
    tools = await mcp_runtime.load_mcp_tools_for_agent(["ghost"], session_factory=_factory([]))
    assert tools == ()


def test_config_hash_cache_keyed_by_slug(monkeypatch: Any) -> None:
    # 前缀拼接与分隔符稳定
    assert mcp_runtime._prefixed("lock-mcp", "issue") == "lock-mcp__issue"
    # 缓存对象可复用
    cache = mcp_client.tool_cache()
    cache._entries.clear()
    assert cache.get("k") is None
    cache.put("k", [_mcp_tool("a")], cache_key="k")
    assert cache.get("k") is not None
