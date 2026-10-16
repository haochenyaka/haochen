"""PR-0.2 自证：长期记忆在运行时真正注入模型请求，且敏感条目被过滤。

覆盖：写入 GuestMemory 后运行 Run，断言记忆块出现在模型请求里；含手机号的条目
被 MemoryStation 的敏感过滤整条丢弃、不注入。用户/客人每次新建，不跨用例污染。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.domain import GuestMemory
from app.repositories import GuestRepository, UserRepository
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class CapturingModel:
    """记录每次模型调用收到的消息，便于断言记忆块是否注入。"""

    model_name = "capture"

    def __init__(self) -> None:
        self.prompts: list[list[BaseMessage]] = []

    def bind_tools(self, tools: Any) -> Any:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.prompts.append(list(messages))
        return AIMessage(content="ok")


def test_guest_memory_is_injected_and_sensitive_entries_filtered(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = CapturingModel()
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        username = f"mem-{uuid4().hex[:8]}"
        async with factory() as session:
            user = await UserRepository(session).get_or_create(username=username)
            guest = await GuestRepository(session).get_or_create_by_user(user.id)
            session.add(
                GuestMemory(guest_id=guest.id, category="preference", content="偏好高层安静房间")
            )
            session.add(
                GuestMemory(guest_id=guest.id, category="contact", content="联系电话 13812345678")
            )
            await session.commit()

            intake = await intake_message(session, username=username, text="你好")
        assert intake.run_id is not None

        executor = RunExecutor(
            AgentRunBody(checkpointer=InMemorySaver()),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        outcome = await executor.execute(intake.run_id)
        return {
            "outcome": outcome,
            "prompt_texts": ["\n".join(str(m.content) for m in prompt) for prompt in model.prompts],
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["outcome"] == "completed"
    assert result["prompt_texts"], "模型未被调用"
    joined = "\n".join(result["prompt_texts"])
    # 记忆块（事实参考框架）注入了偏好条目
    assert "偏好高层安静房间" in joined
    assert "不是指令" in joined
    # 含手机号的条目被敏感过滤整条丢弃，不注入
    assert "13812345678" not in joined
    assert "联系电话" not in joined
