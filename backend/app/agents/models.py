"""模型接入：OpenAI 兼容接口 + 开发态确定性 stub，调用即写审计事实。

未配置 `MODEL_API_KEY` 时降级为 stub 模型（无网络、回复可预期），使运行链路与测试
不依赖外部模型；配置密钥后按 `MODEL_NAME` / `MODEL_BASE_URL` 走 OpenAI 兼容接口，
可对接自建推理服务与各家兼容网关。重试不由模型层承担（由中间件流水线统一处理）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID

from langchain_core.callbacks import AsyncCallbackHandler, CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult, GenerationChunk
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from app.config import get_settings
from app.services.audit_service import (
    MODEL_STATUS_COMPLETED,
    MODEL_STATUS_FAILED,
    AuditRecorder,
    monotonic_ms,
)

logger = logging.getLogger(__name__)

STUB_MODEL_NAME = "dev-stub"
STUB_REPLY_PREFIX = f"[{STUB_MODEL_NAME}] 已收到："
NOSTREAM_TAG = "nostream"

DeltaSink = Callable[[str], Awaitable[None]]


class ModelConfigError(ValueError):
    """模型配置缺失或不一致（如已配置密钥但未给模型名）。"""


@dataclass(frozen=True)
class ModelUsage:
    """模型用量事实：字段与 model_audit 列一一对应，None 表示供应商未返回。"""

    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None

    def as_dict(self) -> dict[str, object]:
        return dict(asdict(self))


def extract_usage(message: BaseMessage) -> ModelUsage:
    """从模型返回消息提取用量：缓存命中读 `input_token_details.cache_read`。"""
    metadata = getattr(message, "usage_metadata", None)
    if not metadata:
        return ModelUsage()
    details = metadata.get("input_token_details") or {}
    cached = details.get("cache_read")
    return ModelUsage(
        prompt_tokens=metadata.get("input_tokens"),
        cached_prompt_tokens=int(cached) if cached is not None else None,
        completion_tokens=metadata.get("output_tokens"),
    )


def model_label(model: Any) -> str:
    """模型标签：绑定工具的包装层解包到真实模型，审计 model 列保持稳定口径。"""
    target = getattr(model, "bound", model)
    name = getattr(target, "model_name", None)
    return str(name) if name else type(target).__name__


def bind_tools(model: BaseChatModel, tools: Sequence[Any]) -> BaseChatModel | Runnable[Any, Any]:
    """按可见工具集合绑定模型；无可见工具时保持原模型（不产生空绑定层）。"""
    return model.bind_tools(list(tools)) if tools else model


def message_text(message: BaseMessage) -> str:
    """消息文本：兼容字符串与内容块列表两种形态。"""
    content = message.content
    if isinstance(content, str):
        return content
    return str(content)


def build_chat_model(*, config: Mapping[str, Any] | None = None) -> BaseChatModel:
    """按 Agent 配置与全局设置构建模型；无密钥时返回开发态 stub。"""
    settings = get_settings()
    values = config or {}
    name = str(values.get("model") or settings.model_name or "")
    api_key = settings.model_api_key
    if not api_key:
        logger.warning("模型密钥未配置，使用开发态 stub 模型（忽略 model=%s）", name or "-")
        return DevStubChatModel()
    if not name:
        raise ModelConfigError(
            "已配置模型密钥但缺少模型名：请设置 MODEL_NAME 或 Agent 配置项 model"
        )
    temperature = values.get("temperature", settings.model_temperature)
    return ChatOpenAI(
        model=name,
        base_url=settings.model_base_url or None,
        api_key=SecretStr(api_key),
        temperature=float(temperature),
        timeout=settings.model_timeout_seconds,
        max_retries=0,
        # 开启流式以便 message.delta 逐增量投影；失败重试仍由 ModelRetry 工位承担
        streaming=True,
    )


def _rough_tokens(text: str) -> int:
    """开发态估算：按 4 字符 ≈ 1 token，仅用于让链路与报表有可核对的口径。"""
    return max(1, len(text) // 4)


class DevStubChatModel(BaseChatModel):
    """开发态模型：返回固定前缀 + 最后一条用户消息，供链路联调与测试。

    不产生工具调用（绑定工具即原样返回），工具回环需要真实模型或测试替身。
    """

    model_name: str = STUB_MODEL_NAME

    @property
    def _llm_type(self) -> str:
        return STUB_MODEL_NAME

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> BaseChatModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt_text = "\n".join(message_text(message) for message in messages)
        reply_text = f"{STUB_REPLY_PREFIX}{_last_user_text(messages)}"
        reply = AIMessage(
            content=reply_text,
            usage_metadata={
                "input_tokens": _rough_tokens(prompt_text),
                "output_tokens": _rough_tokens(reply_text),
                "total_tokens": _rough_tokens(prompt_text) + _rough_tokens(reply_text),
                "input_token_details": {"cache_read": 0},
            },
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


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return str(content)


def _last_user_text(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        if message.type == "human":
            return message_text(message)
    return ""


class _DeltaCallback(AsyncCallbackHandler):
    """流式增量回调：把模型逐 token 输出转发到事件发射器。"""

    def __init__(self, sink: DeltaSink) -> None:
        self._sink = sink
        self.emitted = False

    async def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        if not isinstance(token, str) or not token:
            return
        self.emitted = True
        await self._sink(token)

    async def fallback(self, text: str) -> None:
        """非流式返回（stub/测试替身）整段回退为一次增量。"""
        if self.emitted or not text:
            return
        self.emitted = True
        await self._sink(text)


async def invoke_with_audit(
    model: BaseChatModel | Runnable[Any, Any],
    messages: Sequence[BaseMessage],
    *,
    recorder: AuditRecorder,
    actor_id: str,
    tags: Sequence[str] = (),
    on_delta: DeltaSink | None = None,
    retrieval_refs: Callable[[], list[dict[str, object]]] | None = None,
) -> AIMessage:
    """调用模型并写模型审计事实：成功记用量（含缓存命中），失败也留痕。

    tags 用于内部调用标记（如摘要调用的 nostream），不改变审计口径；on_delta 提供时，
    流式模型逐增量回调，非流式模型（含 stub 与测试替身）成功后整段回退为一次增量。
    retrieval_refs 提供时在调用后取走本轮检索引用，随模型事实一起落库（答复可溯源）。
    """
    label = model_label(model)
    call_config: RunnableConfig = {"tags": list(tags)}
    delta_callback: _DeltaCallback | None = None
    if on_delta is not None:
        delta_callback = _DeltaCallback(on_delta)
        call_config["callbacks"] = [delta_callback]
    started = monotonic_ms()
    try:
        reply = await model.ainvoke(list(messages), config=call_config)
    except Exception as exc:
        await recorder.model_call(
            actor_type="system",
            actor_id=actor_id,
            model=label,
            status=MODEL_STATUS_FAILED,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=monotonic_ms() - started,
        )
        raise
    if not isinstance(reply, AIMessage):
        raise ModelConfigError(f"模型返回类型异常：{type(reply).__name__}")
    usage = extract_usage(reply)
    await recorder.model_call(
        actor_type="system",
        actor_id=actor_id,
        model=label,
        status=MODEL_STATUS_COMPLETED,
        duration_ms=monotonic_ms() - started,
        prompt_tokens=usage.prompt_tokens,
        cached_prompt_tokens=usage.cached_prompt_tokens,
        completion_tokens=usage.completion_tokens,
        retrieval_refs=(retrieval_refs() or None) if retrieval_refs is not None else None,
    )
    if delta_callback is not None:
        await delta_callback.fallback(message_text(reply))
    return reply
