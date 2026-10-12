"""Agent 注册表：扫描 `buildin` 目录自动发现，slug 必填且唯一。

发现规则：`buildin/<agent>/` 子包导入后，收集其中定义的 BaseAgent 具体子类；
包内不定义 Agent 的辅助模块（下划线开头）跳过。发现结果进程内缓存，测试可强制刷新。
"""

from __future__ import annotations

import importlib
import inspect
import logging
import pkgutil
from collections.abc import Iterator
from types import ModuleType

from app.agents.base import BaseAgent

logger = logging.getLogger(__name__)

BUILDIN_MODULE = "app.agents.buildin"


class AgentRegistryError(RuntimeError):
    """Agent 发现失败：slug 缺失或重复。"""


_registry: dict[str, BaseAgent] | None = None


def _iter_agent_classes(module: ModuleType, *, package: str) -> Iterator[type[BaseAgent]]:
    for obj in vars(module).values():
        if not isinstance(obj, type) or not issubclass(obj, BaseAgent) or obj is BaseAgent:
            continue
        if inspect.isabstract(obj):
            continue
        if not obj.__module__.startswith(package):
            continue
        yield obj


def build_registry(package_module: ModuleType) -> dict[str, BaseAgent]:
    """从 buildin 包构建注册表；重复或缺失 slug 立即失败（配置漂移不静默）。"""
    registry: dict[str, BaseAgent] = {}
    for _, module_name, is_package in pkgutil.iter_modules(package_module.__path__):
        if not is_package or module_name.startswith("_"):
            continue
        package = f"{package_module.__name__}.{module_name}"
        module = importlib.import_module(package)
        for agent_cls in _iter_agent_classes(module, package=package):
            slug = agent_cls.slug
            if not slug:
                raise AgentRegistryError(f"{package}.{agent_cls.__name__} 缺少 slug")
            registered = registry.get(slug)
            if registered is not None:
                raise AgentRegistryError(
                    f"Agent slug 重复：{slug}"
                    f"（{type(registered).__name__} 与 {agent_cls.__name__}）"
                )
            registry[slug] = agent_cls()
            logger.info("agent registered: slug=%s name=%s", slug, agent_cls.name)
    return registry


def discover_agents(*, refresh: bool = False) -> dict[str, BaseAgent]:
    global _registry
    if _registry is None or refresh:
        package = importlib.import_module(BUILDIN_MODULE)
        _registry = build_registry(package)
    return _registry


def get_agent(slug: str) -> BaseAgent:
    registry = discover_agents()
    agent = registry.get(slug)
    if agent is None:
        raise AgentRegistryError(f"未注册的 Agent：{slug}")
    return agent


def list_agents() -> list[BaseAgent]:
    return sorted(discover_agents().values(), key=lambda agent: agent.slug)
