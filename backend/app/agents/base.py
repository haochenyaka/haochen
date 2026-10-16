"""BaseAgent 契约：一个 `buildin/<agent>/` 子包即一个 Agent。

类属性声明管理端可见元信息；`context_schema` 驱动配置表单；`build_graph` 在
运行期上下文（含已归一化资源与检查点句柄）就绪后才被调用。
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from app.agents.context import BaseContext, ConfigItem

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from app.agents.runtime import RuntimeContext


class AgentPackageError(RuntimeError):
    """Agent 子包定位失败。"""


class BaseAgent(ABC):
    slug: str = ""
    name: str = ""
    description: str = ""
    context_schema: type[BaseContext] | None = None
    chat_entry: ClassVar[bool] = True
    """是否为客人可选的聊天入口：False 表示该 Agent 只经后台任务/管理端使用（如 qa-auditor），
    接入侧据此在建 Run 之前拒绝非入口 slug，避免建出无可用图的 Run。"""
    default_system_prompt: ClassVar[str] = ""
    """默认系统提示词：管理端未覆盖时的提示词基线（子 Agent 由 own AGENTS.md 补充）。"""
    tool_ids: ClassVar[tuple[str, ...]] = ()
    """本 Agent 声明的工具意图（P1 代码声明；P2 起由配置表单提供并经授权归一化）。"""
    knowledge_base_ids: ClassVar[tuple[str, ...]] = ()
    """本 Agent 声明的知识库代码（与 tool_ids 同级：代码声明，执行处复验）。"""
    skill_ids: ClassVar[tuple[str, ...]] = ()
    """本 Agent 声明的 Skill（声明项 P4 阶段一律预加载，渐进激活随 P5 内容落地）。"""

    @classmethod
    def get_configurable_items(cls) -> list[ConfigItem]:
        """管理端配置表单项：由 context_schema 字段元数据派生。"""
        schema = cls.context_schema
        return schema.get_configurable_items() if schema is not None else []

    @classmethod
    def package_dir(cls) -> Path:
        """Agent 子包目录：AGENTS.md / USER.md 提示词文档的装载位置。"""
        module = sys.modules.get(cls.__module__)
        module_file = getattr(module, "__file__", None)
        if not module_file:
            raise AgentPackageError(f"无法定位 Agent 子包：{cls.__name__}")
        return Path(module_file).resolve().parent

    @abstractmethod
    async def build_graph(self, runtime: RuntimeContext) -> CompiledStateGraph[Any, Any, Any, Any]:
        """装配并编译本 Agent 的图；运行期资源与检查点由 runtime 提供。"""
