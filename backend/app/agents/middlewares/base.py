"""中间件工位契约：固定顺序的装配点（前置按序、后置逆序的洋葱模型）。

工位顺序见机制基线 §3（11 道）；前置钩子按工位顺序执行，后置钩子按逆序执行，
保证凭证打码先于大结果落盘、先于记忆与摘要消费历史。工位只覆写需要的钩子，
未覆写的钩子按直通处理。
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, cast, get_args, get_origin, get_type_hints

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

logger = logging.getLogger(__name__)


class MiddlewareConfigError(ValueError):
    """中间件配置非法：未知工位、未知字段或字段类型不符。"""


class ToolDeniedError(RuntimeError):
    """工具被策略拒绝执行（不重试、不静默跳过）。"""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"工具 {tool_name} 被拒绝：{reason}")
        self.tool_name = tool_name
        self.reason = reason


class ToolApprovalRequired(RuntimeError):
    """工具需人工审批后才能执行；审批等待（interrupt/resume）随 T9 接入。"""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"工具 {tool_name} 需要审批：{reason}")
        self.tool_name = tool_name
        self.reason = reason


class DuplicateToolCallError(RuntimeError):
    """同一 Run 内同参副作用工具已执行或正在执行：重复调用被拦截。"""

    def __init__(self, tool_name: str, key: str) -> None:
        super().__init__(f"工具 {tool_name} 重复调用已拦截（幂等键 {key}）")
        self.tool_name = tool_name
        self.key = key


class SteerSuperseded(RuntimeError):
    """Steer：检测到更新的用户消息，当前 Run 让位给队头新请求。

    由运行执行器按取消收敛（cancel_event 已在触发点置位），新请求经既有接力链路派发。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"当前 Run 被更新的用户消息接管：{reason}")


@dataclass
class ModelCallRequest:
    """一次模型调用的请求：前置工位可改写消息与可见工具集合。"""

    messages: list[BaseMessage]
    tools: Sequence[Any] = ()
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallRequest:
    """一次工具调用的请求：metadata 为注册元数据（skill、dangerous 等）。"""

    name: str
    args: Mapping[str, Any]
    call_id: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """工具执行结果：后置工位可改写（打码、卸载大结果）。"""

    call_id: str
    content: Any
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


ModelCallDelegate = Callable[[], Awaitable[AIMessage]]


def leading_system_count(messages: Sequence[BaseMessage]) -> int:
    """开头连续系统消息的条数：系统提示与记忆注入属于不可折叠区。"""
    count = 0
    for message in messages:
        if isinstance(message, SystemMessage):
            count += 1
        else:
            break
    return count


class Station:
    """工位基类：key 必须与装配顺序表一致，未覆写钩子为直通。"""

    key: ClassVar[str] = ""
    label: ClassVar[str] = ""

    def __init__(self, *, enabled: bool = True) -> None:
        if not self.key:
            raise MiddlewareConfigError(f"{type(self).__name__} 未声明工位键")
        self.enabled = enabled

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        return request

    def allows_tool(self, tool: Any) -> bool:
        """工具可见性判定：默认放行，Skills 等工位覆写（可见与可执行共用同一判据）。"""
        return True

    def redact_tool_args(self, args: dict[str, Any]) -> None:
        """审计与投影入参打码：默认不改写；CredentialGuard 覆写（就地改写副本）。"""
        return None

    async def call_model(self, request: ModelCallRequest, call: ModelCallDelegate) -> AIMessage:
        return await call()

    async def after_model(self, request: ModelCallRequest, reply: AIMessage) -> AIMessage:
        return reply

    async def before_tool(self, call: ToolCallRequest) -> None:
        return None

    def reuse_tool_result(self, call: ToolCallRequest) -> ToolResult | None:
        """已完成的同键副作用调用：返回既有结果复用；默认不复用（ToolGuard 覆写）。"""
        return None

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        return result


def build_station_settings[StationSettingsT](
    settings_cls: type[StationSettingsT], raw: Mapping[str, Any] | None
) -> StationSettingsT:
    """按 dataclass 字段从配置映射构建工位配置：未知字段显式失败。"""
    values = dict(raw or {})
    hints = get_type_hints(settings_cls)
    known = {item.name for item in dataclasses.fields(cast(Any, settings_cls))}
    unknown = sorted(set(values) - known)
    if unknown:
        raise MiddlewareConfigError(f"{settings_cls.__name__} 未知配置项：{unknown}")
    coerced = {name: coerce_config_value(value, hints.get(name)) for name, value in values.items()}
    return settings_cls(**coerced)


def coerce_config_value(value: Any, hint: Any) -> Any:
    """配置值转换：支持标量与 ``tuple[str, ...]`` / ``dict[str, tuple[str, ...]]``。"""
    if hint is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if hint is int:
        return int(value)
    if hint is float:
        return float(value)
    if hint is str:
        return str(value)
    origin = get_origin(hint)
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise MiddlewareConfigError(f"期望数组，得到 {value!r}")
        args = get_args(hint)
        item_hint = args[0] if args else str
        return tuple(coerce_config_value(item, item_hint) for item in value)
    if origin is dict:
        if not isinstance(value, Mapping):
            raise MiddlewareConfigError(f"期望对象，得到 {value!r}")
        args = get_args(hint)
        value_hint = args[1] if len(args) > 1 else None
        return {str(key): coerce_config_value(item, value_hint) for key, item in value.items()}
    return value
