"""Agent 发现与提示词文档装载验证。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.agents.buildin.front_desk.agent import FrontDeskContext
from app.agents.prompts import PROMPT_DOC_MAX_BYTES, load_prompt_docs
from app.agents.registry import (
    AgentRegistryError,
    build_registry,
    discover_agents,
    get_agent,
    list_agents,
)

FAKE_AGENT_SOURCE = """\
from app.agents.base import BaseAgent


class FakeAgent(BaseAgent):
    slug = "dup"
    name = "重复 Agent"

    async def build_graph(self, runtime):
        raise NotImplementedError
"""


def test_discover_agents_finds_front_desk_package() -> None:
    agents = discover_agents(refresh=True)
    assert "front-desk" in agents
    agent = agents["front-desk"]
    assert agent.name == "前台助理"
    assert agent.context_schema is FrontDeskContext
    assert agent.package_dir().name == "front_desk"


def test_list_agents_is_sorted_by_slug() -> None:
    slugs = [agent.slug for agent in list_agents()]
    assert slugs == sorted(slugs)


def test_get_agent_rejects_unknown_slug() -> None:
    with pytest.raises(AgentRegistryError, match="missing-agent"):
        get_agent("missing-agent")


def test_build_registry_rejects_duplicate_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_dir = tmp_path / "fake_buildin"
    for module_name in ("alpha", "beta"):
        module_dir = package_dir / module_name
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text(FAKE_AGENT_SOURCE, encoding="utf-8")
    package = ModuleType("fake_agents_buildin")
    package.__path__ = [str(package_dir)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package.__name__, package)

    with pytest.raises(AgentRegistryError, match="slug 重复"):
        build_registry(package)


def test_load_prompt_docs_concatenates_in_declared_order(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("agent rules", encoding="utf-8")
    (tmp_path / "USER.md").write_text("user context", encoding="utf-8")

    docs = load_prompt_docs(tmp_path)
    assert docs.index("# AGENTS.md") < docs.index("# USER.md")
    assert "agent rules" in docs
    assert "user context" in docs


def test_load_prompt_docs_skips_missing_files(tmp_path: Path) -> None:
    assert load_prompt_docs(tmp_path) == ""


def test_load_prompt_docs_truncates_at_limit(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("a" * (PROMPT_DOC_MAX_BYTES + 128), encoding="utf-8")

    docs = load_prompt_docs(tmp_path)
    assert docs.count("a") == PROMPT_DOC_MAX_BYTES
