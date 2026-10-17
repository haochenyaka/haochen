"""集成测试共用装配：走真实接力路径的消息接入、投递记录器、确定性模型替身与事件总线替身。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.repositories import (
    MessageRepository,
    RequestRepository,
    ThreadRepository,
    UserRepository,
)
from app.services.run_event_bus import RunEventRecord, is_stream_id
from app.services.thread_dispatch import promote_head_if_idle


def force_stub_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """固定走开发态 stub 模型：测试不依赖外部模型与网络。"""
    monkeypatch.setattr(get_settings(), "model_api_key", "")


class ScriptedToolChatModel(BaseChatModel):
    """确定性模型替身：首次调用发出工具调用，看到工具结果后给出最终回复。

    seen_tools 记录每次绑定的可见工具名，用于断言"授权 + Skill 门控"后的模型可见集合。
    """

    model_name: str = "scripted-tool"
    tool_name: str = "test_echo"
    tool_args: dict[str, Any] = Field(default_factory=lambda: {"text": "你好"})
    seen_tools: list[list[str]] = Field(default_factory=list)
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BaseChatModel:
        self.seen_tools.append([str(getattr(item, "name", "?")) for item in tools])
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.call_count += 1
        tool_results = [message for message in messages if isinstance(message, ToolMessage)]
        if tool_results:
            text = "最终回复：" + "；".join(str(message.content) for message in tool_results)
            reply = AIMessage(content=text)
        else:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_args),
                        "id": f"call-{self.call_count}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


class PerTurnToolChatModel(BaseChatModel):
    """确定性模型替身（多轮）：只看**最后一条**消息是不是工具结果。

    与 `ScriptedToolChatModel` 的差别在判据：后者看到任意历史工具结果就给最终回复，多轮
    用例第二轮会因此不再发工具调用；本替身每轮都会真实产生一次工具事实，用于验证多轮
    工具序列的聚合口径。
    """

    model_name: str = "scripted-tool"
    tool_name: str = "test_echo"
    tool_args: dict[str, Any] = Field(default_factory=lambda: {"text": "你好"})
    call_count: int = 0

    @property
    def _llm_type(self) -> str:
        return self.model_name

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.call_count += 1
        if messages and isinstance(messages[-1], ToolMessage):
            reply = AIMessage(content="最终回复：" + str(messages[-1].content))
        else:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tool_name,
                        "args": dict(self.tool_args),
                        "id": f"call-{self.call_count}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=reply)])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return self._generate(messages, stop=stop, **kwargs)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_run(self, run_id: UUID) -> None:
        self.enqueued.append(run_id)

    async def aclose(self) -> None:
        return None


class RecordingRunEventBus:
    """内存事件总线替身：发布即追加速度游标，读取按游标过滤，跟随以毫秒级节奏让出。"""

    def __init__(self) -> None:
        self.streams: dict[UUID, list[RunEventRecord]] = {}
        self.closed = False

    def records(self, run_id: UUID) -> list[RunEventRecord]:
        return list(self.streams.get(run_id, ()))

    async def publish(self, run_id: UUID, event: str, data: Mapping[str, Any]) -> str:
        stream = self.streams.setdefault(run_id, [])
        record = RunEventRecord(id=f"{len(stream) + 1}-0", event=event, data=dict(data))
        stream.append(record)
        return record.id

    async def read(
        self, run_id: UUID, *, after: str | None = None, count: int = 256
    ) -> list[RunEventRecord]:
        return _after_cursor(self.streams.get(run_id, ()), after)[:count]

    async def follow(
        self,
        run_id: UUID,
        *,
        after: str | None = None,
        count: int = 64,
        block_ms: int = 2000,
    ) -> list[RunEventRecord]:
        await asyncio.sleep(min(block_ms, 10) / 1000)
        return _after_cursor(self.streams.get(run_id, ()), after)[:count]

    async def aclose(self) -> None:
        self.closed = True


def _after_cursor(stream: Sequence[RunEventRecord], after: str | None) -> list[RunEventRecord]:
    if not is_stream_id(after):
        return list(stream)
    cursor = _cursor_value(after or "")
    return [record for record in stream if _cursor_value(record.id) > cursor]


def _cursor_value(stream_id: str) -> tuple[int, int]:
    milliseconds, _, sequence = stream_id.partition("-")
    return int(milliseconds), int(sequence or 0)


@dataclass(frozen=True)
class Intake:
    thread_id: UUID
    request_id: UUID
    run_id: UUID | None


async def intake_message(
    session: AsyncSession, *, username: str, text: str = "你好", thread_id: UUID | None = None
) -> Intake:
    """消息与请求落库后，在空闲线程上派发队头 Run（与线上接入同一路径）。"""
    user = await UserRepository(session).get_or_create(username=username)
    if thread_id is None:
        thread = await ThreadRepository(session).create(user_id=user.id, agent_slug="front-desk")
        thread_id = thread.id
    message = await MessageRepository(session).append(
        thread_id=thread_id, role="user", content={"text": text}
    )
    request = await RequestRepository(session).create(
        user_id=user.id, agent_slug="front-desk", thread_id=thread_id, message_id=message.id
    )
    plan = await promote_head_if_idle(session, thread_id)
    await session.commit()
    return Intake(thread_id=thread_id, request_id=request.id, run_id=plan.run_id if plan else None)
