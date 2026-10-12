"""SKILL.md 装载与校验：frontmatter 解析、目录校验、依赖与指令渲染（失败关闭）。

装载规则：
- 内容根不存在 → 空目录（尚未引入 Skill 的部署不因此失败）；
- 子目录缺 SKILL.md、frontmatter 非法、必填字段缺失、slug 与目录名不符、未知字段
  → 显式失败（内容漂移不静默）；
- 依赖指向未装载 Skill 时，由激活闭包展开处对「激活集合 - 可用集合」显式失败
  （根文件缺失不静默降级）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SKILL_FILE = "SKILL.md"

FRONTMATTER_KEYS: frozenset[str] = frozenset(
    {
        "name",
        "slug",
        "description",
        "tool_dependencies",
        "mcp_dependencies",
        "skill_dependencies",
    }
)
REQUIRED_KEYS: frozenset[str] = frozenset({"name", "slug", "description"})
DEPENDENCY_KEYS = ("tool_dependencies", "mcp_dependencies", "skill_dependencies")


class SkillCatalogError(ValueError):
    """Skill 内容非法：缺文件、frontmatter 不合约或字段漂移。"""


@dataclass(frozen=True)
class SkillDocument:
    """一个已装载的 Skill：frontmatter 元信息 + 正文指令。"""

    slug: str
    name: str
    description: str
    tool_dependencies: tuple[str, ...]
    mcp_dependencies: tuple[str, ...]
    skill_dependencies: tuple[str, ...]
    instructions: str
    path: Path


@dataclass(frozen=True)
class SkillCatalog:
    """内容根下的全部 Skill：按 slug 索引，文档只读。"""

    documents: Mapping[str, SkillDocument] = field(default_factory=dict)

    @property
    def slugs(self) -> frozenset[str]:
        return frozenset(self.documents)

    def get(self, slug: str) -> SkillDocument | None:
        return self.documents.get(slug)

    def require(self, slug: str) -> SkillDocument:
        document = self.documents.get(slug)
        if document is None:
            raise SkillCatalogError(f"Skill 未装载：{slug}（已装载：{sorted(self.documents)}）")
        return document

    def dependencies(self) -> dict[str, tuple[str, ...]]:
        """依赖闭包映射：Skill → 其声明的 skill_dependencies。"""
        return {slug: document.skill_dependencies for slug, document in self.documents.items()}

    def render_instructions(self, slugs: Iterable[str]) -> str:
        """激活 Skill 的执行说明段（按 slug 排序，拼进系统提示词）。"""
        sections: list[str] = []
        for slug in sorted(set(slugs)):
            document = self.documents.get(slug)
            if document is None:
                continue
            sections.append(f"## {document.name}（{document.slug}）\n\n{document.instructions}")
        if not sections:
            return ""
        return "# Skill 说明（已激活，按说明执行）\n\n" + "\n\n".join(sections)

    @classmethod
    def load(cls, root: Path) -> SkillCatalog:
        if not root.is_dir():
            logger.warning("Skill 内容根不存在，按空目录处理：%s", root)
            return cls()
        documents: dict[str, SkillDocument] = {}
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            documents[child.name] = _load_document(child)
        return cls(documents=documents)


def default_skills_root() -> Path:
    """默认内容根：与 `app` 包同级（源码树为 backend/skills，容器为 /srv/app/skills）。"""
    return Path(__file__).resolve().parents[3] / "skills"


def load_skill_catalog(root: str | Path | None = None) -> SkillCatalog:
    """按配置装载 Skill 目录：root 为空取默认内容根。"""
    base = Path(root) if root else default_skills_root()
    return SkillCatalog.load(base)


def _load_document(directory: Path) -> SkillDocument:
    slug = directory.name
    path = directory / DEFAULT_SKILL_FILE
    if not path.is_file():
        raise SkillCatalogError(f"Skill 目录缺少 {DEFAULT_SKILL_FILE}：{directory}")
    raw = path.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw, path)

    unknown = sorted(set(meta) - FRONTMATTER_KEYS)
    if unknown:
        raise SkillCatalogError(f"{path} 存在未知 frontmatter 字段：{unknown}")
    missing = sorted(REQUIRED_KEYS - set(meta))
    if missing:
        raise SkillCatalogError(f"{path} 缺少必填 frontmatter 字段：{missing}")
    declared_slug = str(meta["slug"]).strip()
    if declared_slug != slug:
        raise SkillCatalogError(f"{path} 的 slug（{declared_slug}）与目录名（{slug}）不符")
    if not body:
        raise SkillCatalogError(f"{path} 正文为空：Skill 必须给出执行说明")

    dependencies = {key: _dependency_tuple(meta, key, path) for key in DEPENDENCY_KEYS}
    return SkillDocument(
        slug=slug,
        name=str(meta["name"]).strip(),
        description=str(meta["description"]).strip(),
        tool_dependencies=dependencies["tool_dependencies"],
        mcp_dependencies=dependencies["mcp_dependencies"],
        skill_dependencies=dependencies["skill_dependencies"],
        instructions=body,
        path=path,
    )


def _split_frontmatter(raw: str, path: Path) -> tuple[dict[str, object], str]:
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillCatalogError(f"{path} 缺少 YAML frontmatter（首行应为 ---）")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise SkillCatalogError(f"{path} 的 frontmatter 未闭合（缺少结束 ---）")
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except yaml.YAMLError as exc:
        raise SkillCatalogError(f"{path} 的 frontmatter 不是合法 YAML：{exc}") from exc
    if meta is not None and not isinstance(meta, dict):
        raise SkillCatalogError(f"{path} 的 frontmatter 必须是键值映射")
    body = "\n".join(lines[end + 1 :]).strip()
    return dict(meta or {}), body


def _dependency_tuple(meta: Mapping[str, object], key: str, path: Path) -> tuple[str, ...]:
    value = meta.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SkillCatalogError(f"{path} 的 {key} 必须是字符串列表")
    return tuple(value)
