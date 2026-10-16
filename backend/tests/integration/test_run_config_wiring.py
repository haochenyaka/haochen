"""PR-0.1 自证：管理端保存的 Agent 配置在运行时真正生效。

覆盖：config_json.context 的 system_prompt 覆盖进入模型请求；enabled_tools 收窄后
模型可见工具集合随之收窄。写入的 AgentConfig 行在用例结束时清理（库为会话级共享）。
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.models.agent_config import AgentConfig
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class CapturingModel:
    """确定性模型替身：记录每次调用收到的消息，便于断言系统提示是否被覆盖。"""

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


async def _purge_config(factory: SessionFactoryArg, slug: str) -> None:
    async with factory() as session:
        await session.execute(delete(AgentConfig).where(AgentConfig.agent_slug == slug))
        await session.commit()


def test_context_config_override_reaches_model_request(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    model = CapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _purge_config(factory, "front-desk")
        try:
            async with factory() as session:
                session.add(
                    AgentConfig(
                        agent_slug="front-desk",
                        config_json={"context": {"system_prompt": "OVERRIDE_SENTINEL"}},
                    )
                )
                await session.commit()
                intake = await intake_message(session, username="config-wiring-tester", text="你好")
            assert intake.run_id is not None
            executor = RunExecutor(
                AgentRunBody(checkpointer=InMemorySaver()),
                dispatcher=RecordingDispatcher(),
                session_factory=factory,
            )
            outcome = await executor.execute(intake.run_id)
            return {
                "outcome": outcome,
                "system_text": str(model.prompts[0][0].content) if model.prompts else "",
            }
        finally:
            await _purge_config(factory, "front-desk")

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    # 管理端覆盖的 system_prompt 出现在模型请求的首个系统消息里
    assert "OVERRIDE_SENTINEL" in result["system_text"]


def test_enabled_tools_narrowing_limits_visible_tools(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)
    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def alpha_tool() -> str:
        """工具甲。"""
        return "a"

    @builtin_tool(category="test", registry=registry)
    async def beta_tool() -> str:
        """工具乙。"""
        return "b"

    model = CapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("alpha_tool", "beta_tool"))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _purge_config(factory, "front-desk")
        try:
            async with factory() as session:
                session.add(AgentConfig(agent_slug="front-desk", enabled_tools=["alpha_tool"]))
                await session.commit()
                intake = await intake_message(session, username="config-narrow-tester", text="你好")
            assert intake.run_id is not None
            executor = RunExecutor(
                AgentRunBody(
                    checkpointer=InMemorySaver(),
                    tool_registry=registry,
                    resource_policy=StaticResourcePolicy(
                        allowed=frozenset({("tool", "alpha_tool"), ("tool", "beta_tool")})
                    ),
                ),
                dispatcher=RecordingDispatcher(),
                session_factory=factory,
            )
            outcome = await executor.execute(intake.run_id)
            return {"outcome": outcome, "seen_tools": model.seen_tools}
        finally:
            await _purge_config(factory, "front-desk")

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    # 代码声明两个工具，管理端只启用 alpha_tool → 模型只见 alpha_tool
    assert result["seen_tools"] == [["alpha_tool"]]
