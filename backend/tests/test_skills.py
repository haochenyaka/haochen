"""Skill 内容装载验证：frontmatter 校验（失败关闭）、正文渲染与默认内容根。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.skills import (
    DEFAULT_SKILL_FILE,
    SkillCatalog,
    SkillCatalogError,
    default_skills_root,
    load_skill_catalog,
)

VALID_SKILL = """---
name: 结算助手
slug: checkout-flow
description: 退房与结算相关事务。
tool_dependencies:
  - settle_order
mcp_dependencies: []
skill_dependencies: []
---

## 流程

1. 先确认订单状态。
"""


def write_skill(root: Path, slug: str, text: str) -> Path:
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / DEFAULT_SKILL_FILE).write_text(text, encoding="utf-8")
    return directory


def test_catalog_loads_valid_skill(tmp_path: Path) -> None:
    write_skill(tmp_path, "checkout-flow", VALID_SKILL)
    catalog = SkillCatalog.load(tmp_path)
    document = catalog.require("checkout-flow")
    assert document.name == "结算助手"
    assert document.tool_dependencies == ("settle_order",)
    assert document.skill_dependencies == ()
    assert "先确认订单状态" in document.instructions
    assert catalog.dependencies() == {"checkout-flow": ()}
    rendered = catalog.render_instructions({"checkout-flow"})
    assert rendered.startswith("# Skill 说明（已激活，按说明执行）")
    assert "结算助手（checkout-flow）" in rendered


def test_catalog_renders_by_slug_order_and_skips_unknown(tmp_path: Path) -> None:
    write_skill(tmp_path, "b-flow", VALID_SKILL.replace("checkout-flow", "b-flow"))
    write_skill(tmp_path, "a-flow", VALID_SKILL.replace("checkout-flow", "a-flow"))
    catalog = SkillCatalog.load(tmp_path)
    rendered = catalog.render_instructions({"unknown", "b-flow", "a-flow"})
    assert rendered.index("a-flow") < rendered.index("b-flow")
    assert "unknown" not in rendered


def test_catalog_missing_root_is_empty(tmp_path: Path) -> None:
    catalog = SkillCatalog.load(tmp_path / "absent")
    assert catalog.slugs == frozenset()
    assert catalog.render_instructions({"checkin-flow"}) == ""


def test_catalog_rejects_missing_skill_file(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    with pytest.raises(SkillCatalogError, match="缺少"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_unknown_frontmatter_key(tmp_path: Path) -> None:
    write_skill(
        tmp_path, "checkout-flow", VALID_SKILL.replace("mcp_dependencies: []", "extra_key: 1")
    )
    with pytest.raises(SkillCatalogError, match="未知 frontmatter 字段"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_missing_required_key(tmp_path: Path) -> None:
    write_skill(tmp_path, "checkout-flow", VALID_SKILL.replace("name: 结算助手\n", ""))
    with pytest.raises(SkillCatalogError, match="缺少必填"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_slug_directory_mismatch(tmp_path: Path) -> None:
    write_skill(tmp_path, "other-flow", VALID_SKILL)
    with pytest.raises(SkillCatalogError, match="与目录名"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_empty_body(tmp_path: Path) -> None:
    empty_body = "---\nname: 结算助手\nslug: checkout-flow\ndescription: 空正文。\n---\n\n"
    write_skill(tmp_path, "checkout-flow", empty_body)
    with pytest.raises(SkillCatalogError, match="正文为空"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_non_list_dependency(tmp_path: Path) -> None:
    write_skill(tmp_path, "checkout-flow", VALID_SKILL.replace("  - settle_order", "  settle"))
    with pytest.raises(SkillCatalogError, match="必须是字符串列表"):
        SkillCatalog.load(tmp_path)


def test_catalog_rejects_unclosed_frontmatter(tmp_path: Path) -> None:
    write_skill(tmp_path, "checkout-flow", "---\nname: x\nslug: checkout-flow\n")
    with pytest.raises(SkillCatalogError, match="未闭合"):
        SkillCatalog.load(tmp_path)


def test_load_skill_catalog_defaults_to_repo_content_root() -> None:
    catalog = load_skill_catalog("")
    root = default_skills_root()
    assert root == Path(__file__).resolve().parents[1] / "skills"
    assert "checkin-flow" in catalog.slugs
    document = catalog.require("checkin-flow")
    assert document.name == "入住办理"
    assert document.tool_dependencies == ("get_checkin_info", "deliver_room_password")
    assert document.instructions
