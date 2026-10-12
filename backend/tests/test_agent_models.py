"""模型接入验证：stub 降级、OpenAI 兼容装配、用量提取与审计留痕。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from app.agents.models import (
    STUB_MODEL_NAME,
    STUB_REPLY_PREFIX,
    DevStubChatModel,
    ModelConfigError,
    build_chat_model,
    extract_usage,
    invoke_with_audit,
    model_label,
)
from app.config import get_settings
from app.services.audit_service import MODEL_STATUS_COMPLETED, MODEL_STATUS_FAILED


class RecordingRecorder:
    """审计写入口替身：只记录调用参数。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def model_call(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def test_build_chat_model_falls_back_to_stub_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "model_api_key", "")
    model = build_chat_model()
    assert isinstance(model, DevStubChatModel)
    assert model_label(model) == STUB_MODEL_NAME


def test_build_chat_model_uses_openai_compatible_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    monkeypatch.setattr(settings, "model_name", "qwen-plus")
    monkeypatch.setattr(settings, "model_base_url", "https://model.example.invalid/v1")
    monkeypatch.setattr(settings, "model_temperature", 0.3)

    model = build_chat_model()
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "qwen-plus"
    assert model.openai_api_base == "https://model.example.invalid/v1"
    assert model.temperature == 0.3
    assert model.max_retries == 0  # 重试由中间件流水线统一处理


def test_build_chat_model_lets_agent_config_override_model_and_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    monkeypatch.setattr(settings, "model_name", "qwen-plus")

    model = build_chat_model(config={"model": "deepseek-chat", "temperature": 0.9})
    assert isinstance(model, ChatOpenAI)
    assert model.model_name == "deepseek-chat"
    assert model.temperature == 0.9


def test_build_chat_model_requires_model_name_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "model_api_key", "sk-test")
    monkeypatch.setattr(settings, "model_name", "")

    with pytest.raises(ModelConfigError, match="MODEL_NAME"):
        build_chat_model()


def test_stub_model_reply_is_deterministic_and_reports_usage() -> None:
    model = DevStubChatModel()
    reply = asyncio.run(model.ainvoke([HumanMessage(content="你好")]))

    assert isinstance(reply, AIMessage)
    assert reply.content == f"{STUB_REPLY_PREFIX}你好"
    usage = extract_usage(reply)
    assert usage.prompt_tokens is not None and usage.prompt_tokens > 0
    assert usage.completion_tokens is not None and usage.completion_tokens > 0
    assert usage.cached_prompt_tokens == 0


def test_stub_model_answers_with_last_user_message() -> None:
    model = DevStubChatModel()
    reply = asyncio.run(
        model.ainvoke(
            [
                HumanMessage(content="第一句"),
                AIMessage(content="收到"),
                HumanMessage(content="第二句"),
            ]
        )
    )
    assert reply.content == f"{STUB_REPLY_PREFIX}第二句"


def test_extract_usage_reads_cache_hit_tokens() -> None:
    message = AIMessage(
        content="hi",
        usage_metadata={
            "input_tokens": 120,
            "output_tokens": 24,
            "total_tokens": 144,
            "input_token_details": {"cache_read": 96},
        },
    )
    usage = extract_usage(message)
    assert usage.prompt_tokens == 120
    assert usage.cached_prompt_tokens == 96
    assert usage.completion_tokens == 24


def test_extract_usage_without_metadata_reports_unknown() -> None:
    usage = extract_usage(AIMessage(content="hi"))
    assert usage.as_dict() == {
        "prompt_tokens": None,
        "cached_prompt_tokens": None,
        "completion_tokens": None,
    }


def test_invoke_with_audit_records_completed_fact() -> None:
    recorder = RecordingRecorder()
    reply = asyncio.run(
        invoke_with_audit(
            DevStubChatModel(),
            [HumanMessage(content="你好")],
            recorder=recorder,  # type: ignore[arg-type]
            actor_id="worker-1",
        )
    )

    assert reply.content == f"{STUB_REPLY_PREFIX}你好"
    call = recorder.calls[0]
    assert call["model"] == STUB_MODEL_NAME
    assert call["status"] == MODEL_STATUS_COMPLETED
    assert call["actor_type"] == "system"
    assert call["actor_id"] == "worker-1"
    assert call["cached_prompt_tokens"] == 0
    assert isinstance(call["duration_ms"], int)


def test_invoke_with_audit_records_failure_then_reraises() -> None:
    class ExplodingModel(DevStubChatModel):
        async def _agenerate(  # type: ignore[override]
            self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any
        ) -> Any:
            raise RuntimeError("模型不可用")

    recorder = RecordingRecorder()
    with pytest.raises(RuntimeError, match="模型不可用"):
        asyncio.run(
            invoke_with_audit(
                ExplodingModel(),
                [HumanMessage(content="你好")],
                recorder=recorder,  # type: ignore[arg-type]
                actor_id="worker-1",
            )
        )

    call = recorder.calls[0]
    assert call["status"] == MODEL_STATUS_FAILED
    assert "模型不可用" in call["error"]


class StreamingFakeModel(BaseChatModel):
    """流式替身：在 _agenerate 内逐 token 触发回调（同真实流式模型的回调通路）。"""

    pieces: tuple[str, ...] = ("你", "好")

    @property
    def _llm_type(self) -> str:
        return "streaming-fake"

    def _generate(  # type: ignore[override]
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        raise NotImplementedError("流式替身仅支持异步路径")

    async def _agenerate(  # type: ignore[override]
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        text = ""
        for piece in self.pieces:
            if run_manager is not None:
                await run_manager.on_llm_new_token(piece)
            text += piece
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def test_invoke_with_audit_forwards_streaming_deltas_without_duplication() -> None:
    recorder = RecordingRecorder()
    received: list[str] = []

    async def sink(text: str) -> None:
        received.append(text)

    reply = asyncio.run(
        invoke_with_audit(
            StreamingFakeModel(),
            [HumanMessage(content="你好")],
            recorder=recorder,  # type: ignore[arg-type]
            actor_id="worker-1",
            on_delta=sink,
        )
    )

    assert reply.content == "你好"
    assert received == ["你", "好"]  # 流式通路已逐增量，不再整段回退
    assert recorder.calls[0]["status"] == MODEL_STATUS_COMPLETED


def test_invoke_with_audit_falls_back_to_single_delta_for_non_streaming_model() -> None:
    recorder = RecordingRecorder()
    received: list[str] = []

    async def sink(text: str) -> None:
        received.append(text)

    asyncio.run(
        invoke_with_audit(
            DevStubChatModel(),
            [HumanMessage(content="你好")],
            recorder=recorder,  # type: ignore[arg-type]
            actor_id="worker-1",
            on_delta=sink,
        )
    )
    assert received == [f"{STUB_REPLY_PREFIX}你好"]


def test_invoke_with_audit_skips_delta_for_tool_call_only_reply() -> None:
    class ToolCallingModel(DevStubChatModel):
        async def _agenerate(  # type: ignore[override]
            self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any
        ) -> ChatResult:
            reply = AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_current_time", "args": {}, "id": "c1", "type": "tool_call"}
                ],
            )
            return ChatResult(generations=[ChatGeneration(message=reply)])

    recorder = RecordingRecorder()
    received: list[str] = []

    async def sink(text: str) -> None:
        received.append(text)

    asyncio.run(
        invoke_with_audit(
            ToolCallingModel(),
            [HumanMessage(content="现在几点")],
            recorder=recorder,  # type: ignore[arg-type]
            actor_id="worker-1",
            on_delta=sink,
        )
    )
    assert received == []  # 只有工具调用、没有文本时不发射空增量


def test_invoke_with_audit_failure_emits_no_delta() -> None:
    class ExplodingModel(DevStubChatModel):
        async def _agenerate(  # type: ignore[override]
            self, messages: list[BaseMessage], stop: Any = None, **kwargs: Any
        ) -> Any:
            raise RuntimeError("模型不可用")

    received: list[str] = []

    async def sink(text: str) -> None:
        received.append(text)

    with pytest.raises(RuntimeError):
        asyncio.run(
            invoke_with_audit(
                ExplodingModel(),
                [HumanMessage(content="你好")],
                recorder=RecordingRecorder(),  # type: ignore[arg-type]
                actor_id="worker-1",
                on_delta=sink,
            )
        )
    assert received == []
