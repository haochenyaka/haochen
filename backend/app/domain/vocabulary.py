"""基础数据词汇（术语表 §11/§12 冻结）：房态、账号角色与状态取值。"""

from __future__ import annotations

from datetime import timedelta, timezone

HOTEL_TIMEZONE = timezone(timedelta(hours=8), name="UTC+8")
"""酒店所在地时区：与前端、日期窗口校验共用的唯一口径。"""

USER_ROLES: frozenset[str] = frozenset({"customer", "staff", "admin", "manager", "operator"})
ACCOUNT_STATUSES: frozenset[str] = frozenset({"active", "disabled"})
ROOM_STATUSES: frozenset[str] = frozenset({"available", "occupied", "cleaning", "maintenance"})
STAFF_POSITIONS: frozenset[str] = frozenset(
    {"housekeeping", "front_desk", "concierge", "maintenance", "management"}
)
ATTENDANCE_DIRECTIONS: frozenset[str] = frozenset({"check_in", "check_out"})
ATTENDANCE_REVIEW_STATUSES: frozenset[str] = frozenset(
    {"auto_pass", "manual_review", "approved", "rejected"}
)
