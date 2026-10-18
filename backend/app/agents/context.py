"""Agent 可配置项（配置表单链路）与运行期资源归一化。

管理端链路：`BaseAgent.get_configurable_items()` → Agent 详情 → 前端表单 →
`config_json.context` → `get_graph` 装配；`BaseContext.from_config` 是配置回到
运行期的唯一入口，未知键显式失败（不静默吞掉配置漂移）。

资源归一化：`prepare_agent_runtime_context` 在 Graph 创建前按当前用户权限裁剪
工具/知识库/MCP/Skills。**资源快照不是授权替代品**：工具执行处仍须复验。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, Literal, Protocol, Self
from uuid import UUID

logger = logging.getLogger(__name__)

CONFIG_METADATA_KEY = "config"

ConfigKind = Literal["text", "textarea", "number", "boolean", "select", "json"]
ResourceKind = Literal["tool", "knowledge_base", "mcp_server", "skill"]


class AgentConfigError(ValueError):
    """配置与 context_schema 不匹配（未知键、类型不符）。"""


@dataclass(frozen=True)
class ConfigItem:
    """管理端表单项：字段元数据派生，前端按 kind 渲染控件。"""

    key: str
    label: str
    kind: ConfigKind = "text"
    default: Any = None
    options: tuple[str, ...] = ()
    help_text: str | None = None
    required: bool = False
    group: str = "basic"


def config_item(
    *,
    label: str,
    kind: ConfigKind = "text",
    options: tuple[str, ...] = (),
    help_text: str | None = None,
    required: bool = False,
    group: str = "basic",
) -> dict[str, dict[str, Any]]:
    """字段元数据工厂：作为 `field(metadata=config_item(...))` 使用。"""
    return {
        CONFIG_METADATA_KEY: {
            "label": label,
            "kind": kind,
            "options": options,
            "help_text": help_text,
            "required": required,
            "group": group,
        }
    }


@dataclass
class BaseContext:
    """Agent 配置基类：子类用 dataclass 字段声明可配置项与默认值。"""

    @classmethod
    def get_configurable_items(cls) -> list[ConfigItem]:
        items: list[ConfigItem] = []
        for config_field in fields(cls):
            meta = config_field.metadata.get(CONFIG_METADATA_KEY)
            if not meta:
                continue
            items.append(
                ConfigItem(
                    key=config_field.name,
                    default=_field_default(config_field),
                    **meta,
                )
            )
        return items

    @classmethod
    def from_config(cls, raw: Mapping[str, Any] | None) -> Self:
        """把管理端配置转为上下文实例；未知键失败（配置漂移不静默）。"""
        values = dict(raw or {})
        known = {config_field.name for config_field in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise AgentConfigError(f"{cls.__name__} 不支持的配置键：{', '.join(unknown)}")
        return cls(**values)


def _field_default(config_field: Any) -> Any:
    if config_field.default is not MISSING:
        return config_field.default
    if config_field.default_factory is not MISSING:
        return config_field.default_factory()
    return None


@dataclass(frozen=True)
class ResourceRef:
    kind: ResourceKind
    id: str


@dataclass(frozen=True)
class ResourceSnapshot:
    """按权限裁剪后的资源快照（按种类分组，供 Graph 装配读取）。"""

    tools: tuple[ResourceRef, ...] = ()
    knowledge_bases: tuple[ResourceRef, ...] = ()
    mcp_servers: tuple[ResourceRef, ...] = ()
    skills: tuple[ResourceRef, ...] = ()

    def ids(self, kind: ResourceKind) -> tuple[str, ...]:
        return tuple(ref.id for ref in self._bucket(kind))

    def _bucket(self, kind: ResourceKind) -> tuple[ResourceRef, ...]:
        match kind:
            case "tool":
                return self.tools
            case "knowledge_base":
                return self.knowledge_bases
            case "mcp_server":
                return self.mcp_servers
            case "skill":
                return self.skills


class ResourcePolicy(Protocol):
    """资源授权判定：实现方按当前用户权限回答，异常应向上抛（不静默放行）。"""

    async def authorizes(self, ref: ResourceRef) -> bool: ...


class DenyAllResourcePolicy:
    """默认策略：无任何授权资源（失败关闭）。P2 接入权限数据后替换。"""

    async def authorizes(self, ref: ResourceRef) -> bool:
        return False


@dataclass(frozen=True)
class StaticResourcePolicy:
    """显式白名单策略：测试与灰度期使用。"""

    allowed: frozenset[tuple[str, str]] = frozenset()

    async def authorizes(self, ref: ResourceRef) -> bool:
        return (ref.kind, ref.id) in self.allowed


@dataclass(frozen=True)
class DeclaredBuiltinPolicy:
    """代码声明放行策略：Agent 代码声明的内置工具、知识库与 Skill 放行，其余一律失败关闭。

    P2 权限数据落地前由 Agent 代码声明承担（放行集合随代码评审，不由运行期配置扩大）；
    快照只决定"模型可见"——读类工具执行处复验存在与可见范围，写类工具还须复验归属、
    状态与时间窗（见订单工具与服务层）。
    """

    declared_tools: frozenset[str] = frozenset()
    knowledge_bases: frozenset[str] = frozenset()
    skills: frozenset[str] = frozenset()
    mcp_servers: frozenset[str] = frozenset()

    async def authorizes(self, ref: ResourceRef) -> bool:
        if ref.kind == "tool":
            return ref.id in self.declared_tools
        if ref.kind == "knowledge_base":
            return ref.id in self.knowledge_bases
        if ref.kind == "skill":
            return ref.id in self.skills
        if ref.kind == "mcp_server":
            return ref.id in self.mcp_servers
        return False


@dataclass(frozen=True)
class AgentResourceRequest:
    """Agent 期望挂载的资源（来自管理端配置，未经授权）。"""

    refs: tuple[ResourceRef, ...] = field(default_factory=tuple)


async def prepare_agent_runtime_context(
    *,
    agent_slug: str,
    user_id: UUID,
    requested: AgentResourceRequest,
    policy: ResourcePolicy | None = None,
) -> ResourceSnapshot:
    """Graph 创建前按权限归一化资源：未授权资源被剔除并留痕。"""
    active_policy = policy if policy is not None else DenyAllResourcePolicy()
    kept: list[ResourceRef] = []
    denied: list[str] = []
    for ref in requested.refs:
        if await active_policy.authorizes(ref):
            kept.append(ref)
        else:
            denied.append(f"{ref.kind}:{ref.id}")
    if denied:
        logger.warning(
            "agent resources denied: agent=%s user=%s refs=%s",
            agent_slug,
            user_id,
            ",".join(denied),
        )
    return ResourceSnapshot(
        tools=tuple(ref for ref in kept if ref.kind == "tool"),
        knowledge_bases=tuple(ref for ref in kept if ref.kind == "knowledge_base"),
        mcp_servers=tuple(ref for ref in kept if ref.kind == "mcp_server"),
        skills=tuple(ref for ref in kept if ref.kind == "skill"),
    )
