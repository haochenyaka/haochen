"""Skills 内容机制：SKILL.md 目录装载、依赖闭包与指令渲染（机制基线 §2.6）。

内容根随服务端发布（`backend/skills/`，容器内 `/srv/app/skills`）：一个子目录一个 Skill，
`SKILL.md` 的 YAML frontmatter 声明名称、描述与依赖，正文是给模型的执行说明。
Agent 代码声明 `skill_ids` 后经资源归一化进入运行期；P4 阶段声明项一律预加载，
渐进激活（读 SKILL.md 后开放工具）随 P5 首个渐进 Skill 落地。
"""

from app.agents.skills.catalog import (
    DEFAULT_SKILL_FILE,
    SkillCatalog,
    SkillCatalogError,
    SkillDocument,
    default_skills_root,
    load_skill_catalog,
)

__all__ = [
    "DEFAULT_SKILL_FILE",
    "SkillCatalog",
    "SkillCatalogError",
    "SkillDocument",
    "default_skills_root",
    "load_skill_catalog",
]
