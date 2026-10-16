"""qa-auditor 质检员：离线跑批的后台 Agent，不提供聊天入口。

职责落在领域任务（`qa.audit`）与 `QaAuditorService`：扫描增量会话产出自动标注与质量信号，
不阻断业务。本类只承担「登记」——让质检员出现在管理端 Agent 列表并暴露可配置项（扫描
窗口、文档过期阈值、低召回判定阈值），配置由 `qa.audit` 处理器读取后交给服务使用。

`chat_entry=False`：客人不能在会话里指定本 Agent，接入侧在建 Run 之前就拒绝；`build_graph`
不给可用图，仅为契约占位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.agents.base import AgentPackageError, BaseAgent
from app.agents.context import BaseContext, config_item

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from app.agents.runtime import RuntimeContext


@dataclass
class QaAuditorContext(BaseContext):
    """质检跑批参数：与 `QaAuditorService` 的构造参数一一对应。"""

    scan_window_hours: int = field(
        default=24,
        metadata=config_item(label="扫描窗口（小时）", kind="number", group="scan"),
    )
    stale_days: int = field(
        default=180,
        metadata=config_item(label="文档过期阈值（天）", kind="number", group="scan"),
    )
    low_recall_threshold: float = field(
        default=0.7,
        metadata=config_item(label="低召回判定阈值", kind="number", group="quality"),
    )


class QaAuditorAgent(BaseAgent):
    slug = "qa-auditor"
    name = "质检员"
    description = "离线跑批的会话质检与数据质量检查（无聊天入口，由 qa.audit 后台任务驱动）"
    context_schema = QaAuditorContext
    chat_entry = False
    tool_ids = ()
    knowledge_base_ids = ()

    async def build_graph(self, runtime: RuntimeContext) -> CompiledStateGraph[Any, Any, Any, Any]:
        raise AgentPackageError("qa-auditor 无聊天入口：请经 qa.audit 后台任务驱动")


__all__ = ["QaAuditorAgent", "QaAuditorContext"]
