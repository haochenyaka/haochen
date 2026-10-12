"""Skill 内容注入集成验证（真实 PostgreSQL + 完整 Run 链路）。

场景：front-desk 声明 checkin-flow。资源策略放行 Skill 时，激活 Skill 的正文指令
拼进模型请求的首条系统消息；不放行时快照裁剪为无 Skill，提示词不含 Skill 段。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class PromptCapturingModel(BaseChatModel):
    """确定性模型替身：记录首条系统消息后直接收尾，不发工具调用。"""

    model_name: str = "prompt-capture"
    seen_prompts: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def bind_tools(self, tools: Any, **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        if messages and isinstance(messages[0], SystemMessage):
            self.seen_prompts.append(str(messages[0].content))
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="您好，请问有什么可以帮您？"))]
        )

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


async def _run_body(factory: SessionFactoryArg, *, allow_skill: bool) -> str:
    # 每用例独立 InMemorySaver：进程级检查点连接池绑定首个事件循环，跨用例复用会触锁冲突
    from langgraph.checkpoint.memory import InMemorySaver

    allowed = frozenset({("skill", "checkin-flow")}) if allow_skill else frozenset()
    executor = RunExecutor(
        AgentRunBody(
            checkpointer=InMemorySaver(), resource_policy=StaticResourcePolicy(allowed=allowed)
        ),
        dispatcher=RecordingDispatcher(),
        session_factory=factory,
    )
    async with factory() as session:
        intake = await intake_message(session, username=f"skill-content-{uuid4().hex[:8]}")
    assert intake.run_id is not None
    return await executor.execute(intake.run_id)


def test_active_skill_instructions_land_in_system_prompt(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = PromptCapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    outcome = run_with_session_factory(
        runtime_db_url, lambda factory: _run_body(factory, allow_skill=True)
    )
    assert outcome == "completed"
    assert model.seen_prompts
    prompt = model.seen_prompts[0]
    assert "# Skill 说明（已激活，按说明执行）" in prompt
    assert "入住办理（checkin-flow）" in prompt
    assert "密码只在当次回复中展示" in prompt


def test_unauthorized_skill_is_excluded_from_prompt(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = PromptCapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    outcome = run_with_session_factory(
        runtime_db_url, lambda factory: _run_body(factory, allow_skill=False)
    )
    assert outcome == "completed"
    assert model.seen_prompts
    prompt = model.seen_prompts[0]
    assert "# Skill 说明" not in prompt
    assert "checkin-flow" not in prompt
