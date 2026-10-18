"""PR-M2 自证：管理端启用的 MCP 服务器工具在运行时注入模型可见集合。

用替身 MCP server 行与工具，不依赖真实远程 MCP。覆盖：启用 → 模型可见 MCP 工具；
未启用 → 不可见（负向）；resource_policy 放行 mcp_server 快照。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import tool as make_tool
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.mcp import runtime as mcp_runtime
from app.models.agent_config import AgentConfig
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class CapturingModel:
    model_name = "capture"

    def __init__(self) -> None:
        self.prompts: list[list[BaseMessage]] = []
        self.seen_tools: list[list[str]] = []

    def bind_tools(self, tools: Any) -> Any:
        self.seen_tools.append([str(getattr(item, "name", "?")) for item in tools])
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        return AIMessage(content="ok")


def _mcp_tool(name: str) -> Any:
    @make_tool
    def _placeholder() -> str:
        """placeholder"""
        return "ok"

    _placeholder.name = name
    _placeholder.metadata = {
        "category": "mcp",
        "display_name": name,
        "dangerous": True,
        "read_only": False,
        "skill": None,
        "credential_delivery": False,
        "mcp_server": "lock-mcp",
    }
    return _placeholder


async def _purge_config(factory: SessionFactoryArg, slug: str) -> None:
    async with factory() as session:
        await session.execute(delete(AgentConfig).where(AgentConfig.agent_slug == slug))
        await session.commit()


def _inject_fake_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_loader(_slugs: Any, **_kwargs: Any) -> tuple[Any, ...]:
        return (_mcp_tool("lock-mcp__read_status"),)

    monkeypatch.setattr(mcp_runtime, "load_mcp_tools_for_agent", _fake_loader)


def test_enabled_mcp_server_tools_reach_model(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    _inject_fake_mcp(monkeypatch)
    model = CapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _purge_config(factory, "front-desk")
        try:
            async with factory() as session:
                session.add(AgentConfig(agent_slug="front-desk", enabled_mcp_servers=["lock-mcp"]))
                await session.commit()
                intake = await intake_message(session, username="mcp-wiring-on", text="你好")
            executor = RunExecutor(
                AgentRunBody(checkpointer=InMemorySaver()),
                dispatcher=RecordingDispatcher(),
                session_factory=factory,
            )
            outcome = await executor.execute(intake.run_id)  # type: ignore[arg-type]
            return {"outcome": outcome, "seen_tools": model.seen_tools}
        finally:
            await _purge_config(factory, "front-desk")

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    visible = {name for batch in result["seen_tools"] for name in batch}
    assert "lock-mcp__read_status" in visible, "启用的 MCP 工具应对模型可见"


def test_disabled_mcp_server_tools_not_visible(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    _inject_fake_mcp(monkeypatch)
    model = CapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _purge_config(factory, "front-desk")
        try:
            # 不写 enabled_mcp_servers → 不注入任何 MCP 工具
            async with factory() as session:
                intake = await intake_message(session, username="mcp-wiring-off", text="你好")
            executor = RunExecutor(
                AgentRunBody(checkpointer=InMemorySaver()),
                dispatcher=RecordingDispatcher(),
                session_factory=factory,
            )
            outcome = await executor.execute(intake.run_id)  # type: ignore[arg-type]
            return {"outcome": outcome, "seen_tools": model.seen_tools}
        finally:
            await _purge_config(factory, "front-desk")

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    visible = {name for batch in result["seen_tools"] for name in batch}
    assert "lock-mcp__read_status" not in visible, "未启用的 MCP 工具不应可见"
