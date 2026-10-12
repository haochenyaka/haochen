"""仓储共用工具。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Result


def affected_rows(result: Result[Any]) -> int:
    """守卫式 UPDATE 的受影响行数（ORM execute 实际返回 CursorResult）。"""
    return int(cast("CursorResult[Any]", result).rowcount)
