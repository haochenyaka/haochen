"""Agent 配置管理接口：Agent 发现列表/详情、配置保存、工具与 Skill 浏览。

权限：admin 可读写，manager 只读。
数据库字段 null 表示使用 agent 代码声明默认值，不冗余保存。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.agent_config_service import (
    AgentConfigDTO,
    AgentConfigService,
    SkillDTO,
    ToolDTO,
)

router = APIRouter(prefix="/admin/agent-config", tags=["admin-agent-config"])

guard = require_roles("admin", "manager")
write_guard = require_roles("admin")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]
WriteGuardDep = Annotated[Identity, Depends(write_guard)]


# ── Response models ──────────────────────────────────────────────


class ConfigItemModel(BaseModel):
    key: str
    label: str
    kind: str
    default: object | None = None
    options: list[str] = []
    help_text: str | None = None
    required: bool = False
    group: str = "basic"


class AgentDetailModel(BaseModel):
    slug: str
    name: str
    description: str
    config_items: list[ConfigItemModel] = []
    tool_ids: list[str] = []
    skill_ids: list[str] = []
    knowledge_base_ids: list[str] = []
    configured: bool = False
    config: dict[str, object] | None = None
    enabled_tools: list[str] | None = None
    enabled_skills: list[str] | None = None
    enabled_knowledge_bases: list[str] | None = None


class ToolModel(BaseModel):
    name: str
    display_name: str
    description: str
    category: str
    skill: str | None = None
    dangerous: bool = False
    read_only: bool = True


class SkillModel(BaseModel):
    slug: str
    name: str
    description: str
    tool_dependencies: list[str] = []
    mcp_dependencies: list[str] = []
    skill_dependencies: list[str] = []


class SaveAgentConfigBody(BaseModel):
    config_json: dict[str, object] | None = None
    enabled_tools: list[str] | None = None
    enabled_skills: list[str] | None = None
    enabled_knowledge_bases: list[str] | None = None


# ── Helpers ──────────────────────────────────────────────────────


def _to_dto(dto: AgentConfigDTO) -> AgentDetailModel:
    return AgentDetailModel(
        slug=dto.slug,
        name=dto.name,
        description=dto.description,
        config_items=[ConfigItemModel(**item) for item in dto.config_items],
        tool_ids=list(dto.tool_ids),
        skill_ids=list(dto.skill_ids),
        knowledge_base_ids=list(dto.knowledge_base_ids),
        configured=dto.configured,
        config=dto.config,
        enabled_tools=dto.enabled_tools,
        enabled_skills=dto.enabled_skills,
        enabled_knowledge_bases=dto.enabled_knowledge_bases,
    )


# ── Routes ───────────────────────────────────────────────────────


@router.get("/agents", response_model=list[AgentDetailModel])
async def list_agents(
    session: SessionDep,
    _identity: GuardDep,
) -> list[AgentDetailModel]:
    svc = AgentConfigService(session)
    agents = await svc.list_agents()
    return [_to_dto(a) for a in agents]


@router.get("/agents/{slug}", response_model=AgentDetailModel)
async def get_agent_detail(
    slug: str,
    session: SessionDep,
    _identity: GuardDep,
) -> AgentDetailModel:
    svc = AgentConfigService(session)
    try:
        detail = await svc.get_agent_detail(slug)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _to_dto(detail)


@router.put("/agents/{slug}", response_model=AgentDetailModel)
async def save_agent_config(
    slug: str,
    body: SaveAgentConfigBody,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> AgentDetailModel:
    svc = AgentConfigService(session)
    try:
        detail = await svc.save_agent_config(
            slug,
            config_json=body.config_json,
            enabled_tools=body.enabled_tools,
            enabled_skills=body.enabled_skills,
            enabled_knowledge_bases=body.enabled_knowledge_bases,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )
    return _to_dto(detail)


@router.delete("/agents/{slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_config(
    slug: str,
    session: SessionDep,
    _identity: WriteGuardDep,
) -> None:
    svc = AgentConfigService(session)
    try:
        await svc.delete_agent_config(slug)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.get("/tools", response_model=list[ToolModel])
async def list_tools(
    session: SessionDep,
    _identity: GuardDep,
) -> list[ToolModel]:
    svc = AgentConfigService(session)
    tools = await svc.list_tools()
    return [
        ToolModel(
            name=t.name,
            display_name=t.display_name,
            description=t.description,
            category=t.category,
            skill=t.skill,
            dangerous=t.dangerous,
            read_only=t.read_only,
        )
        for t in tools
    ]


@router.get("/skills", response_model=list[SkillModel])
async def list_skills(
    session: SessionDep,
    _identity: GuardDep,
) -> list[SkillModel]:
    svc = AgentConfigService(session)
    skills = await svc.list_skills()
    return [
        SkillModel(
            slug=s.slug,
            name=s.name,
            description=s.description,
            tool_dependencies=list(s.tool_dependencies),
            mcp_dependencies=list(s.mcp_dependencies),
            skill_dependencies=list(s.skill_dependencies),
        )
        for s in skills
    ]