"""工位 4 Skills：激活闭包与工具可见性门控（内容随 P4 的 checkin-flow 提供）。

激活集合 = preload（含 Agent 声明的 Skill 资源）+ 运行期激活，再按依赖闭包展开；
请求激活的 Skill 不在可用集合内时显式失败（根文件缺失不静默降级）。未激活 Skill 的工具
不出现在模型可见工具集合里；"可执行"层面的门控由工具注册表在调用处复验（T8）。
激活 Skill 的正文指令由装配方经 `instructions` 取走，拼进系统提示词。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.middlewares.base import ModelCallRequest, Station
from app.agents.skills import SkillCatalog

TOOL_SKILL_METADATA_KEY = "skill"


class SkillActivationError(ValueError):
    """激活了不可用的 Skill 或依赖闭包缺少根。"""


@dataclass(frozen=True)
class SkillsSettings:
    enabled: bool = True
    preload: tuple[str, ...] = ()
    activated: tuple[str, ...] = ()
    dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)


def resolve_active_skills(
    *,
    activated: Sequence[str],
    preload: Sequence[str],
    dependencies: Mapping[str, Sequence[str]],
) -> frozenset[str]:
    """展开依赖闭包：预加载与显式激活同权，依赖递归纳入。"""
    active: set[str] = set()
    pending = [*preload, *activated]
    while pending:
        skill = pending.pop()
        if skill in active:
            continue
        active.add(skill)
        pending.extend(dependencies.get(skill, ()))
    return frozenset(active)


class SkillsStation(Station):
    key = "skills"
    label = "Skills 技能激活"

    def __init__(
        self,
        *,
        settings: SkillsSettings | None = None,
        catalog: SkillCatalog | None = None,
        preload: Sequence[str] = (),
    ) -> None:
        resolved = settings or SkillsSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._catalog = catalog
        merged_preload = tuple(dict.fromkeys([*resolved.preload, *preload]))
        dependencies: dict[str, tuple[str, ...]] = {
            **(catalog.dependencies() if catalog is not None else {}),
            **resolved.dependencies,
        }
        self.active = resolve_active_skills(
            activated=resolved.activated,
            preload=merged_preload,
            dependencies=dependencies,
        )
        available = catalog.slugs if catalog is not None else frozenset()
        unknown = sorted(self.active - available)
        if unknown:
            raise SkillActivationError(
                f"激活的 Skill 不在可用集合内：{unknown}（可用：{sorted(available)}）"
            )

    @property
    def instructions(self) -> str:
        """激活 Skill 的正文指令：拼进系统提示词，装配方负责取用位置。"""
        if self._catalog is None or not self.active:
            return ""
        return self._catalog.render_instructions(self.active)

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        if request.tools:
            request.tools = tuple(tool for tool in request.tools if self.allows_tool(tool))
        return request

    def allows_tool(self, tool: Any) -> bool:
        metadata = getattr(tool, "metadata", None) or {}
        skill = metadata.get(TOOL_SKILL_METADATA_KEY)
        return not skill or str(skill) in self.active
