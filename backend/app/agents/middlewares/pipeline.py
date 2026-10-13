"""中间件流水线装配：固定 11 道工位顺序（机制基线 §3），配置驱动启停与参数。

前置钩子按工位顺序执行、后置钩子按逆序执行（洋葱模型），模型调用由工位包裹；
工位顺序不做运行期变更，启停与参数由 Agent 配置的 middleware 段覆盖全局设置默认值。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.middlewares.base import (
    MiddlewareConfigError,
    ModelCallDelegate,
    ModelCallRequest,
    Station,
    ToolCallRequest,
    ToolResult,
    build_station_settings,
)
from app.agents.middlewares.credential_guard import CredentialGuardSettings, CredentialGuardStation
from app.agents.middlewares.filesystem import FilesystemSettings, FilesystemStation
from app.agents.middlewares.memory import MemoryProvider, MemorySettings, MemoryStation
from app.agents.middlewares.model_retry import ModelRetrySettings, ModelRetryStation, Sleeper
from app.agents.middlewares.patch_tool_calls import PatchToolCallsSettings, PatchToolCallsStation
from app.agents.middlewares.skills import SkillsSettings, SkillsStation
from app.agents.middlewares.steer import (
    CancelSignal as SteerCancelSignal,
)
from app.agents.middlewares.steer import (
    SteerSettings,
    SteerStation,
)
from app.agents.middlewares.summary import (
    SummarizationStation,
    SummaryDelegate,
    SummarySettings,
    build_default_summarizer,
)
from app.agents.middlewares.token_usage import TokenUsageSettings, TokenUsageStation
from app.agents.middlewares.tool_approval import (
    ApprovalPolicy,
    ToolApprovalSettings,
    ToolApprovalStation,
)
from app.agents.middlewares.tool_guard import ToolGuardSettings, ToolGuardStation
from app.agents.skills import SkillCatalog
from app.config import get_settings
from app.services.audit_service import AuditRecorder

STATION_ORDER: tuple[str, ...] = (
    "steer",
    "filesystem",
    "credential_guard",
    "skills",
    "memory",
    "summarization",
    "patch_tool_calls",
    "model_retry",
    "tool_guard",
    "token_usage",
    "tool_approval",
)

_SETTINGS_BY_KEY: dict[str, type[Any]] = {
    "steer": SteerSettings,
    "filesystem": FilesystemSettings,
    "credential_guard": CredentialGuardSettings,
    "skills": SkillsSettings,
    "memory": MemorySettings,
    "summarization": SummarySettings,
    "patch_tool_calls": PatchToolCallsSettings,
    "model_retry": ModelRetrySettings,
    "tool_guard": ToolGuardSettings,
    "token_usage": TokenUsageSettings,
    "tool_approval": ToolApprovalSettings,
}


def _default_filesystem_settings() -> FilesystemSettings:
    settings = get_settings()
    return FilesystemSettings(
        offload_threshold_bytes=settings.large_result_threshold_bytes,
        preview_chars=settings.large_result_preview_chars,
    )


def _default_summary_settings() -> SummarySettings:
    settings = get_settings()
    return SummarySettings(
        threshold_tokens=settings.summary_threshold,
        keep_messages=settings.summary_keep_messages,
    )


def _default_retry_settings() -> ModelRetrySettings:
    settings = get_settings()
    return ModelRetrySettings(
        max_attempts=settings.model_retry_max_attempts,
        initial_backoff_seconds=settings.model_retry_backoff_seconds,
    )


@dataclass(frozen=True)
class MiddlewareConfig:
    """流水线配置：字段与工位一一对应，缺省取全局设置（阈值、重试、卸载上限）。"""

    steer: SteerSettings = field(default_factory=SteerSettings)
    filesystem: FilesystemSettings = field(default_factory=_default_filesystem_settings)
    credential_guard: CredentialGuardSettings = field(default_factory=CredentialGuardSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    summarization: SummarySettings = field(default_factory=_default_summary_settings)
    patch_tool_calls: PatchToolCallsSettings = field(default_factory=PatchToolCallsSettings)
    model_retry: ModelRetrySettings = field(default_factory=_default_retry_settings)
    tool_guard: ToolGuardSettings = field(default_factory=ToolGuardSettings)
    token_usage: TokenUsageSettings = field(default_factory=TokenUsageSettings)
    tool_approval: ToolApprovalSettings = field(default_factory=ToolApprovalSettings)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> MiddlewareConfig:
        """按工位键解析 middleware 配置段：未知工位或未知字段显式失败。

        未给出的字段取全局设置派生的缺省值，给出的字段按工位粒度覆盖。
        """
        values = dict(raw or {})
        unknown = sorted(set(values) - set(STATION_ORDER))
        if unknown:
            raise MiddlewareConfigError(f"未知中间件工位配置：{unknown}")
        defaults = cls()
        overrides = {
            key: build_station_settings(
                _SETTINGS_BY_KEY[key],
                {**dataclasses.asdict(getattr(defaults, key)), **values[key]},
            )
            for key in values
        }
        return dataclasses.replace(defaults, **overrides)


class MiddlewarePipeline:
    """固定顺序的工位流水线：前置按序、后置逆序，模型调用由工位包裹。"""

    def __init__(self, stations: Sequence[Station]) -> None:
        self._stations = tuple(stations)
        keys = [station.key for station in self._stations]
        if len(set(keys)) != len(keys):
            raise MiddlewareConfigError(f"工位键重复：{keys}")
        self._by_key = {station.key: station for station in self._stations}

    @property
    def stations(self) -> tuple[Station, ...]:
        return self._stations

    def station(self, key: str) -> Station:
        return self._by_key[key]

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        for station in self._stations:
            if station.enabled:
                request = await station.prepare_model_call(request)
        return request

    def allows_tool(self, tool: Any) -> bool:
        """工具判据：请求准备（模型可见）与工具执行（可调用）共用同一判据。"""
        return all(station.allows_tool(tool) for station in self._stations if station.enabled)

    def redact_tool_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """审计与事件投影用的入参副本：打码工位就地改写，工具执行仍用模型原始入参。"""
        masked = dict(args)
        for station in self._stations:
            if station.enabled:
                station.redact_tool_args(masked)
        return masked

    async def invoke_model(self, request: ModelCallRequest, base: ModelCallDelegate) -> AIMessage:
        delegate = base
        for station in reversed(self._stations):
            if station.enabled:
                delegate = _wrap_call(station, request, delegate)
        return await delegate()

    async def after_model(self, request: ModelCallRequest, reply: AIMessage) -> AIMessage:
        for station in reversed(self._stations):
            if station.enabled:
                reply = await station.after_model(request, reply)
        return reply

    async def before_tool(self, call: ToolCallRequest) -> None:
        for station in self._stations:
            if station.enabled:
                await station.before_tool(call)

    def reuse_tool_result(self, call: ToolCallRequest) -> ToolResult | None:
        """复用判定：同键已完成时返回既有结果（先命中为准），调用方不再执行工具。"""
        for station in self._stations:
            if not station.enabled:
                continue
            reused = station.reuse_tool_result(call)
            if reused is not None:
                return reused
        return None

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        for station in reversed(self._stations):
            if station.enabled:
                result = await station.after_tool(call, result)
        return result

    @property
    def token_usage(self) -> dict[str, object] | None:
        station = self._by_key.get("token_usage")
        if isinstance(station, TokenUsageStation) and station.enabled:
            return station.summary()
        return None

    @property
    def summarization_events(self) -> list[dict[str, Any]]:
        station = self._by_key.get("summarization")
        if isinstance(station, SummarizationStation) and station.enabled:
            return station.events
        return []

    @property
    def credential_redactions(self) -> list[dict[str, Any]]:
        station = self._by_key.get("credential_guard")
        if isinstance(station, CredentialGuardStation) and station.enabled:
            return station.redactions
        return []

    @property
    def tool_guard_events(self) -> list[dict[str, Any]]:
        station = self._by_key.get("tool_guard")
        if isinstance(station, ToolGuardStation) and station.enabled:
            return station.events
        return []

    @property
    def skill_prompt(self) -> str:
        """激活 Skill 的正文指令（无激活或未装载内容目录时为空串）。"""
        station = self._by_key.get("skills")
        if isinstance(station, SkillsStation) and station.enabled:
            return station.instructions
        return ""


def _wrap_call(
    station: Station, request: ModelCallRequest, inner: ModelCallDelegate
) -> ModelCallDelegate:
    async def call() -> AIMessage:
        return await station.call_model(request, inner)

    return call


def build_middleware_pipeline(
    *,
    config: MiddlewareConfig | Mapping[str, Any] | None = None,
    workdir: Path | None = None,
    thread_id: UUID | None = None,
    user_id: UUID | None = None,
    run_id: UUID | None = None,
    recorder: AuditRecorder | None = None,
    actor_id: str = "",
    skills: SkillCatalog | None = None,
    preload_skills: Iterable[str] = (),
    memory_provider: MemoryProvider | None = None,
    summary_delegate: SummaryDelegate | None = None,
    approval_policy: ApprovalPolicy | None = None,
    sleeper: Sleeper | None = None,
    steer_session_factory: async_sessionmaker[AsyncSession] | None = None,
    steer_cancel_signal: SteerCancelSignal | None = None,
    approval_interrupt_enabled: bool = False,
) -> MiddlewarePipeline:
    """装配 11 道工位；顺序与基线一致，否则显式失败。"""
    resolved = (
        config if isinstance(config, MiddlewareConfig) else MiddlewareConfig.from_mapping(config)
    )
    token_usage_station = TokenUsageStation(settings=resolved.token_usage)
    summarizer = summary_delegate or build_default_summarizer(
        recorder=recorder, actor_id=actor_id, on_usage=token_usage_station.record_usage
    )
    pipeline = MiddlewarePipeline(
        [
            SteerStation(
                settings=resolved.steer,
                session_factory=steer_session_factory,
                thread_id=thread_id,
                cancel_signal=steer_cancel_signal,
            ),
            FilesystemStation(settings=resolved.filesystem, workdir=workdir),
            CredentialGuardStation(settings=resolved.credential_guard),
            SkillsStation(settings=resolved.skills, catalog=skills, preload=tuple(preload_skills)),
            MemoryStation(
                settings=resolved.memory,
                provider=memory_provider,
                user_id=user_id,
                thread_id=thread_id,
            ),
            SummarizationStation(
                settings=resolved.summarization,
                workdir=workdir,
                thread_id=str(thread_id or ""),
                summarizer=summarizer,
            ),
            PatchToolCallsStation(settings=resolved.patch_tool_calls),
            ModelRetryStation(settings=resolved.model_retry, sleeper=sleeper),
            ToolGuardStation(settings=resolved.tool_guard, run_id=run_id),
            token_usage_station,
            ToolApprovalStation(
                settings=resolved.tool_approval,
                policy=approval_policy,
                interrupt_enabled=approval_interrupt_enabled,
            ),
        ]
    )
    keys = tuple(station.key for station in pipeline.stations)
    if keys != STATION_ORDER:
        raise MiddlewareConfigError(f"工位顺序与基线不符：{keys}")
    return pipeline
