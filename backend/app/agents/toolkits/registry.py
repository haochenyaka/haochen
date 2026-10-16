"""工具注册骨架：内置工具注册、Skill 门控与 ToolNode 装配。

"可执行"与"模型可见"分离：注册进 ToolNode 只说明工具可执行，是否对模型开放由请求准备
处的门控决定（未激活 Skill 的工具不出现在 schema 里），执行处再以注册元数据复验一次
（模型点名了也不执行，fail-closed）。工具调用事实（状态、耗时、入参）由包装层写
tool_audit；入参与事件投影用的是打码后的副本（pipeline.redact_tool_args），工具执行仍用
模型原始入参，事实与投影都不携带凭证明文。副作用工具的重复调用由 ToolGuard 工位拦下或
复用既有结果，幂等键经 ToolResult.metadata 随事实落库（tool_audit.idempotency_key）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool
from langchain_core.tools import tool as create_tool
from langgraph.prebuilt import ToolNode

from app.agents.middlewares.base import (
    DuplicateToolCallError,
    ToolApprovalRejected,
    ToolApprovalRequired,
    ToolCallRequest,
    ToolResult,
)
from app.agents.middlewares.credential_guard import CREDENTIAL_DELIVERY_METADATA_KEY
from app.agents.middlewares.filesystem import content_text
from app.agents.middlewares.pipeline import MiddlewarePipeline
from app.agents.middlewares.skills import TOOL_SKILL_METADATA_KEY
from app.agents.middlewares.tool_approval import DANGEROUS_METADATA_KEY
from app.agents.middlewares.tool_guard import (
    IDEMPOTENCY_KEY_METADATA_KEY,
    READ_ONLY_METADATA_KEY,
)
from app.services.audit_service import (
    TOOL_STATUS_COMPLETED,
    TOOL_STATUS_FAILED,
    AuditRecorder,
    monotonic_ms,
)
from app.services.run_events import RunEventEmitter

logger = logging.getLogger(__name__)

CATEGORY_METADATA_KEY = "category"
DISPLAY_NAME_METADATA_KEY = "display_name"
ERROR_TEXT_LIMIT = 500
BLOCKED_DUPLICATE_REASON = (
    "重复调用已拦截：同一 Run 内同参数的副作用工具已执行或正在执行，未重复执行。"
)


class ToolRegistrationError(ValueError):
    """工具注册或选取非法：重名、未注册。"""


def tool_metadata(tool: BaseTool | None) -> Mapping[str, Any]:
    """工具注册元数据：门控判定与管理端展示读同一份口径。"""
    return getattr(tool, "metadata", None) or {}


class ToolRegistry:
    """进程内注册表：注册元数据随工具对象保存，选取按授权资源 id 收敛。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(
        self,
        tool: BaseTool,
        *,
        category: str = "general",
        skill: str | None = None,
        dangerous: bool = False,
        read_only: bool = True,
        display_name: str | None = None,
        credential_delivery: bool = False,
    ) -> BaseTool:
        if tool.name in self._tools:
            raise ToolRegistrationError(f"工具重名：{tool.name}")
        metadata = dict(tool_metadata(tool))
        metadata.update(
            {
                CATEGORY_METADATA_KEY: category,
                TOOL_SKILL_METADATA_KEY: skill,
                DANGEROUS_METADATA_KEY: dangerous,
                READ_ONLY_METADATA_KEY: read_only,
                DISPLAY_NAME_METADATA_KEY: display_name or tool.name,
                CREDENTIAL_DELIVERY_METADATA_KEY: credential_delivery,
            }
        )
        tool.metadata = metadata
        self._tools[tool.name] = tool
        return tool

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def get(self, name: str) -> BaseTool:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolRegistrationError(f"工具未注册：{name}（已注册：{self.names()}）")
        return tool

    def select(self, ids: Iterable[str]) -> tuple[BaseTool, ...]:
        """按授权资源 id 选取（顺序跟随调用方）；未注册 id 显式失败。"""
        return tuple(self.get(name) for name in ids)

    def read_only_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, tool in self._tools.items()
            if tool_metadata(tool).get(READ_ONLY_METADATA_KEY)
        )

    def describe(self) -> list[dict[str, Any]]:
        """管理端展示口径：稳定注册信息（名称、分类、描述、Skill 归属、危险标记）。"""
        described: list[dict[str, Any]] = []
        for tool in self._tools.values():
            metadata = tool_metadata(tool)
            described.append(
                {
                    "name": tool.name,
                    "display_name": metadata.get(DISPLAY_NAME_METADATA_KEY) or tool.name,
                    "description": tool.description,
                    "category": metadata.get(CATEGORY_METADATA_KEY, "general"),
                    "skill": metadata.get(TOOL_SKILL_METADATA_KEY),
                    "dangerous": bool(metadata.get(DANGEROUS_METADATA_KEY)),
                    "read_only": bool(metadata.get(READ_ONLY_METADATA_KEY)),
                    "credential_delivery": bool(metadata.get(CREDENTIAL_DELIVERY_METADATA_KEY)),
                }
            )
        return described


_default_registry: ToolRegistry | None = None


def default_registry() -> ToolRegistry:
    """进程级注册表：内置工具模块在包导入时按固定顺序注册（见 toolkits/__init__）。"""
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry


def builtin_tool(
    *,
    category: str = "general",
    skill: str | None = None,
    dangerous: bool = False,
    read_only: bool = True,
    display_name: str | None = None,
    credential_delivery: bool = False,
    registry: ToolRegistry | None = None,
) -> Callable[[Callable[..., Any]], BaseTool]:
    """内置工具装饰器：`@tool` 语义 + 稳定注册元数据 + 进入注册表。

    credential_delivery 标记下发类工具：回包中的一次性明文由 CredentialGuard 取走并
    打码落库副本（见下发通道）。
    """

    def decorate(func: Callable[..., Any]) -> BaseTool:
        created = create_tool(func)
        (registry or default_registry()).register(
            created,
            category=category,
            skill=skill,
            dangerous=dangerous,
            read_only=read_only,
            display_name=display_name,
            credential_delivery=credential_delivery,
        )
        return created

    return decorate


def build_tool_node(
    *,
    tools: Sequence[BaseTool],
    pipeline: MiddlewarePipeline,
    recorder: AuditRecorder,
    events: RunEventEmitter | None = None,
    name: str = "tools",
) -> ToolNode:
    """装配 ToolNode：门控复验 → 复用判定 → before_tool → 执行 → after_tool → tool_audit 事实。

    工具自身抛错转为模型可见的错误结果（Run 不因单个工具失败而中断）；审批等待发射
    interrupt 事件后向上抛出（interrupt/resume 语义随 P2+ 接入）；门控拒绝与审批拒绝
    是策略结论，不写工具事实。副作用工具的重复调用由 ToolGuard 处置：执行中拦下（写失败
    事实 + 模型可见错误结果），已完成则复用既有结果（事实同为 completed、耗时 0、同幂等
    键）。事件是投影：tool.call 在调用前发射，tool.result 在调用后与审计同步发射。
    """

    async def awrap_tool_call(request: Any, execute: Any) -> Any:
        call = request.tool_call
        tool_name = str(call["name"])
        call_id = str(call["id"])
        args = pipeline.redact_tool_args(call.get("args") or {})
        tool = request.tool
        metadata = dict(tool_metadata(tool))
        pipeline_call = ToolCallRequest(
            name=tool_name, args=args, call_id=call_id, metadata=metadata
        )
        if events is not None:
            await events.tool_call(tool=tool_name, call_id=call_id, args=args)
        if tool is not None and not pipeline.allows_tool(tool):
            return await _reject(
                recorder,
                pipeline_call,
                "所属 Skill 未激活，工具未向模型开放也不可调用",
                events=events,
            )
        reused = pipeline.reuse_tool_result(pipeline_call)
        if reused is not None:
            return await _reuse(recorder, pipeline, pipeline_call, reused, events=events)
        try:
            await pipeline.before_tool(pipeline_call)
        except ToolApprovalRejected as exc:
            # 人工拒绝：回一条拒绝消息进对话（模型据此改道或转人工），Run 不失败
            return await _blocked_approval(recorder, pipeline_call, exc, events=events)
        except ToolApprovalRequired as exc:
            if events is not None:
                await events.interrupt(tool=tool_name, call_id=call_id, reason=exc.reason)
            raise
        except DuplicateToolCallError as exc:
            return await _blocked_duplicate(recorder, pipeline_call, exc, events=events)
        started = monotonic_ms()
        try:
            outcome = await execute(request)
        except Exception as exc:
            failure_error = f"{type(exc).__name__}: {exc}"
            failed = await pipeline.after_tool(
                pipeline_call,
                ToolResult(
                    call_id=call_id,
                    content=f"工具执行失败（{type(exc).__name__}）：{exc}",
                    is_error=True,
                ),
            )
            duration_ms = monotonic_ms() - started
            await recorder.tool_call(
                tool_name=tool_name,
                status=TOOL_STATUS_FAILED,
                args_digest=args,
                idempotency_key=_result_key(failed.metadata),
                duration_ms=duration_ms,
                error=failure_error,
            )
            if events is not None:
                await events.tool_result(
                    tool=tool_name,
                    call_id=call_id,
                    status=TOOL_STATUS_FAILED,
                    duration_ms=duration_ms,
                    error=failure_error,
                )
            logger.warning("工具执行失败：tool=%s error=%s", tool_name, exc)
            return _error_message(tool_name, call_id, content_text(failed.content))
        if not isinstance(outcome, ToolMessage):
            duration_ms = monotonic_ms() - started
            await recorder.tool_call(
                tool_name=tool_name,
                status=TOOL_STATUS_COMPLETED,
                args_digest=args,
                duration_ms=duration_ms,
            )
            if events is not None:
                await events.tool_result(
                    tool=tool_name,
                    call_id=call_id,
                    status=TOOL_STATUS_COMPLETED,
                    duration_ms=duration_ms,
                )
            return outcome
        result = await pipeline.after_tool(
            pipeline_call,
            ToolResult(
                call_id=call_id,
                content=outcome.content,
                is_error=outcome.status == "error",
                metadata=dict(metadata),
            ),
        )
        if result.content is not outcome.content:
            outcome = outcome.model_copy(update={"content": result.content})
        duration_ms = monotonic_ms() - started
        status = TOOL_STATUS_FAILED if result.is_error else TOOL_STATUS_COMPLETED
        error = _error_text(result) if result.is_error else None
        await recorder.tool_call(
            tool_name=tool_name,
            status=status,
            args_digest=args,
            idempotency_key=_result_key(result.metadata),
            duration_ms=duration_ms,
            error=error,
        )
        if events is not None:
            await events.tool_result(
                tool=tool_name,
                call_id=call_id,
                status=status,
                duration_ms=duration_ms,
                error=error,
            )
        return outcome

    return ToolNode(
        list(tools), name=name, awrap_tool_call=awrap_tool_call, handle_tool_errors=False
    )


async def _reject(
    recorder: AuditRecorder,
    call: ToolCallRequest,
    reason: str,
    *,
    events: RunEventEmitter | None = None,
) -> ToolMessage:
    await recorder.tool_call(
        tool_name=call.name,
        status=TOOL_STATUS_FAILED,
        args_digest=dict(call.args),
        duration_ms=0,
        error=reason,
    )
    if events is not None:
        await events.tool_result(
            tool=call.name,
            call_id=call.call_id,
            status=TOOL_STATUS_FAILED,
            duration_ms=0,
            error=reason,
        )
    logger.warning("工具调用被拒：tool=%s reason=%s", call.name, reason)
    return _error_message(call.name, call.call_id, reason)


async def _reuse(
    recorder: AuditRecorder,
    pipeline: MiddlewarePipeline,
    call: ToolCallRequest,
    reused: ToolResult,
    *,
    events: RunEventEmitter | None = None,
) -> ToolMessage:
    """复用既有结果：仍经 after_tool 复走打码链，事实记 completed、耗时 0、同幂等键。"""
    result = await pipeline.after_tool(call, reused)
    key = _result_key(result.metadata)
    status = TOOL_STATUS_FAILED if result.is_error else TOOL_STATUS_COMPLETED
    error = _error_text(result) if result.is_error else None
    await recorder.tool_call(
        tool_name=call.name,
        status=status,
        args_digest=dict(call.args),
        idempotency_key=key,
        duration_ms=0,
        error=error,
    )
    if events is not None:
        await events.tool_result(
            tool=call.name, call_id=call.call_id, status=status, duration_ms=0, error=error
        )
    logger.info("副作用调用复用既有结果：tool=%s key=%s", call.name, key)
    return ToolMessage(
        content=result.content,
        name=call.name,
        tool_call_id=call.call_id,
        status="error" if result.is_error else "success",
    )


async def _blocked_duplicate(
    recorder: AuditRecorder,
    call: ToolCallRequest,
    exc: DuplicateToolCallError,
    *,
    events: RunEventEmitter | None = None,
) -> ToolMessage:
    """重复调用拦截：写失败事实（同幂等键）与结果事件，返回模型可见的错误结果。"""
    await recorder.tool_call(
        tool_name=call.name,
        status=TOOL_STATUS_FAILED,
        args_digest=dict(call.args),
        idempotency_key=exc.key,
        duration_ms=0,
        error=BLOCKED_DUPLICATE_REASON,
    )
    if events is not None:
        await events.tool_result(
            tool=call.name,
            call_id=call.call_id,
            status=TOOL_STATUS_FAILED,
            duration_ms=0,
            error=BLOCKED_DUPLICATE_REASON,
        )
    logger.warning("重复调用被拦截：tool=%s key=%s", call.name, exc.key)
    return _error_message(call.name, call.call_id, BLOCKED_DUPLICATE_REASON)


async def _blocked_approval(
    recorder: AuditRecorder,
    call: ToolCallRequest,
    exc: ToolApprovalRejected,
    *,
    events: RunEventEmitter | None = None,
) -> ToolMessage:
    """审批被拒：写失败事实与结果事件，返回模型可见的拒绝回执（Run 继续）。"""
    reason = f"人工审批未通过：{exc.reason}"
    await recorder.tool_call(
        tool_name=call.name,
        status=TOOL_STATUS_FAILED,
        args_digest=dict(call.args),
        duration_ms=0,
        error=reason,
    )
    if events is not None:
        await events.tool_result(
            tool=call.name,
            call_id=call.call_id,
            status=TOOL_STATUS_FAILED,
            duration_ms=0,
            error=reason,
        )
    logger.warning("工具审批被拒：tool=%s", call.name)
    return _error_message(call.name, call.call_id, reason)


def _result_key(metadata: Mapping[str, Any]) -> str | None:
    key = metadata.get(IDEMPOTENCY_KEY_METADATA_KEY)
    return str(key) if key else None


def _error_message(tool_name: str, call_id: str, reason: str) -> ToolMessage:
    return ToolMessage(
        content=f"错误：{reason}",
        name=tool_name,
        tool_call_id=call_id,
        status="error",
    )


def _error_text(result: ToolResult) -> str:
    return content_text(result.content)[:ERROR_TEXT_LIMIT]
