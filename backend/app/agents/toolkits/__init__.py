"""工具注册层：内置工具注册表、Skill 门控与 ToolNode 装配。

内置工具模块在这里按固定顺序导入：模块导入即注册，因此包导入顺序就是注册顺序；
子模块被任何路径直接导入时都会先执行本包初始化，注册顺序与导入路径无关。
新增领域工具模块时在此显式登记，模块间有相互导入时被依赖方排在前面。
"""

from app.agents.toolkits import (  # noqa: F401,I001  （顺序即注册顺序）
    knowledge,
    orders,
    checkin,
    room_service,
    handoff,
    system,
)
from app.agents.toolkits.registry import (
    CATEGORY_METADATA_KEY,
    DISPLAY_NAME_METADATA_KEY,
    READ_ONLY_METADATA_KEY,
    ToolRegistrationError,
    ToolRegistry,
    build_tool_node,
    builtin_tool,
    default_registry,
    tool_metadata,
)

__all__ = [
    "CATEGORY_METADATA_KEY",
    "DISPLAY_NAME_METADATA_KEY",
    "READ_ONLY_METADATA_KEY",
    "ToolRegistrationError",
    "ToolRegistry",
    "build_tool_node",
    "builtin_tool",
    "default_registry",
    "tool_metadata",
]
