"""子 Agent 共用配置形态：系统提示词 / 模型 / 采样温度。

子 Agent 的最终系统提示词 = 本上下文里的 system_prompt（管理端可覆盖）；留空时由装配方
回退到子 Agent 的 `default_system_prompt` 与其 AGENTS.md 文档。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.context import BaseContext, config_item


@dataclass
class SubAgentContext(BaseContext):
    system_prompt: str = field(
        default="",
        metadata=config_item(label="系统提示词", kind="textarea", group="prompt"),
    )
    model: str = field(
        default="",
        metadata=config_item(label="模型名（留空用全局默认）", group="model"),
    )
    temperature: float = field(
        default=0.2,
        metadata=config_item(label="采样温度", kind="number", group="model"),
    )


__all__ = ["SubAgentContext"]
