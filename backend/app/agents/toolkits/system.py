"""系统域内置只读工具：P1 最小 Agent 的演示工具（领域工具随 P3–P5 逐个落地）。

只读、无外部依赖、不产生副作用，用于打通"模型 → ToolNode → 审计"链路；回答与
"今天/明天/现在"相关的问题以工具返回为准，不凭模型记忆。
"""

from __future__ import annotations

from datetime import datetime

from app.agents.toolkits.registry import builtin_tool
from app.domain.vocabulary import HOTEL_TIMEZONE, WEEKDAY_LABELS


@builtin_tool(category="system", read_only=True, display_name="当前时间")
async def get_current_time() -> str:
    """获取当前日期与时间（酒店所在地为 UTC+8，含星期）。

    回答涉及"今天、明天、现在"等时间判断的问题前调用。
    """
    now = datetime.now(HOTEL_TIMEZONE)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')} 星期{WEEKDAY_LABELS[now.weekday()]}（UTC+8）"
