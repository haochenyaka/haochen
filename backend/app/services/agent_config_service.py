"""Agent 配置服务：桥接 Agent 注册表/ToolRegistry/SkillCatalog 与 DB 持久化配置。

管理端通过本服务实现：
1. 列出已发现的 Agent 及其配置项（来自代码声明）
2. 查看/保存 Agent 配置覆盖（config_json）
3. 管理 Agent 启用的工具、技能、知识库
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import discover_agents, get_agent
from app.agents.skills.catalog import load_skill_catalog
from app.agents.toolkits.registry import default_registry
from app.models.agent_config import AgentConfig

CONTEXT_SECTION = "context"
MIDDLEWARE_SECTION = "middleware"


@dataclass(frozen=True)
class AgentRuntimeConfig:
    """运行期配置：`context` 段交 `BaseContext.from_config`，`middleware` 段交
    `MiddlewareConfig.from_mapping`；`enabled_*` 为管理端选择的资源子集（None 表示
    沿用 Agent 代码声明）。运行期由 `run_body` 读取并装配。"""

    context: dict[str, object] = field(default_factory=dict)
    middleware: dict[str, object] = field(default_factory=dict)
    enabled_tools: tuple[str, ...] | None = None
    enabled_skills: tuple[str, ...] | None = None
    enabled_knowledge_bases: tuple[str, ...] | None = None


def normalize_config_json(
    raw: Mapping[str, object] | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """兼容解析 config_json：嵌套 `{context, middleware}` 或旧扁平（整段视为 context）。

    历史数据是扁平形状（前端曾直接写表单键值），迁移期内两种都要读得出来；扁平输入无
    中间件段，返回空 dict。段的非对象值显式失败（配置漂移不静默）。"""
    values = dict(raw or {})
    if CONTEXT_SECTION in values or MIDDLEWARE_SECTION in values:
        context = values.get(CONTEXT_SECTION) or {}
        middleware = values.get(MIDDLEWARE_SECTION) or {}
        if not isinstance(context, Mapping) or not isinstance(middleware, Mapping):
            raise ValueError("config_json 的 context / middleware 段必须是对象")
        return dict(context), dict(middleware)
    return values, {}


@dataclass
class AgentConfigDTO:
    """管理端可见的 Agent 完整信息（代码声明 + DB 覆盖）。"""

    slug: str
    name: str
    description: str
    config_items: list[dict[str, Any]]
    tool_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]
    knowledge_base_ids: tuple[str, ...]
    configured: bool
    config: dict[str, object] | None
    enabled_tools: list[str] | None
    enabled_skills: list[str] | None
    enabled_knowledge_bases: list[str] | None


@dataclass
class ToolDTO:
    name: str
    display_name: str
    description: str
    category: str
    skill: str | None
    dangerous: bool
    read_only: bool


@dataclass
class SkillDTO:
    slug: str
    name: str
    description: str
    tool_dependencies: tuple[str, ...]
    mcp_dependencies: tuple[str, ...]
    skill_dependencies: tuple[str, ...]


class AgentConfigService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_agents(self) -> list[AgentConfigDTO]:
        """列出所有已发现的 Agent，合并 DB 配置覆盖状态。"""
        agents = discover_agents()
        db_configs = await self._load_all_configs()
        results: list[AgentConfigDTO] = []
        for slug, agent in sorted(agents.items()):
            config_items = agent.get_configurable_items()
            db_conf = db_configs.get(slug)
            results.append(
                AgentConfigDTO(
                    slug=slug,
                    name=agent.name,
                    description=agent.description,
                    config_items=[_item_to_dict(item) for item in config_items],
                    tool_ids=agent.tool_ids,
                    skill_ids=agent.skill_ids,
                    knowledge_base_ids=agent.knowledge_base_ids,
                    configured=db_conf is not None,
                    config=db_conf.config_json if db_conf else None,
                    enabled_tools=db_conf.enabled_tools if db_conf else None,
                    enabled_skills=db_conf.enabled_skills if db_conf else None,
                    enabled_knowledge_bases=db_conf.enabled_knowledge_bases if db_conf else None,
                )
            )
        return results

    async def get_agent_detail(self, slug: str) -> AgentConfigDTO:
        """获取单个 Agent 详情。"""
        agent = get_agent(slug)
        db_conf = await self._load_config(slug)
        config_items = agent.get_configurable_items()
        return AgentConfigDTO(
            slug=slug,
            name=agent.name,
            description=agent.description,
            config_items=[_item_to_dict(item) for item in config_items],
            tool_ids=agent.tool_ids,
            skill_ids=agent.skill_ids,
            knowledge_base_ids=agent.knowledge_base_ids,
            configured=db_conf is not None,
            config=db_conf.config_json if db_conf else None,
            enabled_tools=db_conf.enabled_tools if db_conf else None,
            enabled_skills=db_conf.enabled_skills if db_conf else None,
            enabled_knowledge_bases=db_conf.enabled_knowledge_bases if db_conf else None,
        )

    async def save_agent_config(
        self,
        slug: str,
        *,
        config_json: dict[str, object] | None = None,
        enabled_tools: list[str] | None = None,
        enabled_skills: list[str] | None = None,
        enabled_knowledge_bases: list[str] | None = None,
    ) -> AgentConfigDTO:
        """保存 Agent 配置覆盖。传入 None 的字段保留 DB 既有值（不覆盖）。"""
        agent = get_agent(slug)
        stmt = select(AgentConfig).where(AgentConfig.agent_slug == slug)
        result = await self._session.execute(stmt)
        db_conf = result.scalar_one_or_none()

        if db_conf is None:
            db_conf = AgentConfig(agent_slug=slug)
            self._session.add(db_conf)

        if config_json is not None:
            db_conf.config_json = config_json or None
        if enabled_tools is not None:
            db_conf.enabled_tools = enabled_tools or None
        if enabled_skills is not None:
            db_conf.enabled_skills = enabled_skills or None
        if enabled_knowledge_bases is not None:
            db_conf.enabled_knowledge_bases = enabled_knowledge_bases or None

        await self._session.commit()
        await self._session.refresh(db_conf)

        config_items = agent.get_configurable_items()
        return AgentConfigDTO(
            slug=slug,
            name=agent.name,
            description=agent.description,
            config_items=[_item_to_dict(item) for item in config_items],
            tool_ids=agent.tool_ids,
            skill_ids=agent.skill_ids,
            knowledge_base_ids=agent.knowledge_base_ids,
            configured=True,
            config=db_conf.config_json,
            enabled_tools=db_conf.enabled_tools,
            enabled_skills=db_conf.enabled_skills,
            enabled_knowledge_bases=db_conf.enabled_knowledge_bases,
        )

    async def delete_agent_config(self, slug: str) -> None:
        """删除 Agent 配置覆盖（恢复代码声明默认值）。"""
        get_agent(slug)  # 验证 agent 存在
        stmt = select(AgentConfig).where(AgentConfig.agent_slug == slug)
        result = await self._session.execute(stmt)
        db_conf = result.scalar_one_or_none()
        if db_conf is not None:
            await self._session.delete(db_conf)
            await self._session.commit()

    async def list_tools(self) -> list[ToolDTO]:
        """列出所有已注册工具。"""
        registry = default_registry()
        described = registry.describe()
        return [
            ToolDTO(
                name=item["name"],
                display_name=item["display_name"],
                description=item["description"],
                category=item["category"],
                skill=item["skill"],
                dangerous=item["dangerous"],
                read_only=item["read_only"],
            )
            for item in described
        ]

    async def list_skills(self) -> list[SkillDTO]:
        """列出所有可用 Skill。"""
        catalog = load_skill_catalog()
        return [
            SkillDTO(
                slug=doc.slug,
                name=doc.name,
                description=doc.description,
                tool_dependencies=doc.tool_dependencies,
                mcp_dependencies=doc.mcp_dependencies,
                skill_dependencies=doc.skill_dependencies,
            )
            for doc in sorted(catalog.documents.values(), key=lambda d: d.slug)
        ]

    async def load_runtime_config(self, slug: str) -> AgentRuntimeConfig:
        """读取某 Agent 的运行期配置：无覆盖记录时返回全默认（空段、None 资源）。"""
        db_conf = await self._load_config(slug)
        if db_conf is None:
            return AgentRuntimeConfig()
        context, middleware = normalize_config_json(db_conf.config_json)
        return AgentRuntimeConfig(
            context=context,
            middleware=middleware,
            enabled_tools=tuple(db_conf.enabled_tools) if db_conf.enabled_tools else None,
            enabled_skills=tuple(db_conf.enabled_skills) if db_conf.enabled_skills else None,
            enabled_knowledge_bases=(
                tuple(db_conf.enabled_knowledge_bases) if db_conf.enabled_knowledge_bases else None
            ),
        )

    async def _load_config(self, slug: str) -> AgentConfig | None:
        stmt = select(AgentConfig).where(AgentConfig.agent_slug == slug)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def _load_all_configs(self) -> dict[str, AgentConfig]:
        stmt = select(AgentConfig)
        result = await self._session.execute(stmt)
        return {row.agent_slug: row for row in result.scalars().all()}


def _item_to_dict(item: Any) -> dict[str, Any]:
    return {
        "key": item.key,
        "label": item.label,
        "kind": item.kind,
        "default": item.default,
        "options": list(item.options) if item.options else [],
        "help_text": item.help_text,
        "required": item.required,
        "group": item.group,
    }
