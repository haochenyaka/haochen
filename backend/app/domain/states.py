"""领域状态机：合法迁移的唯一口径（术语表 §2/§4），实现与测试都必须引用本模块。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ORDER_STATUSES: tuple[str, ...] = (
    "created",
    "pending_payment",
    "paid",
    "checked_in",
    "checkout_pending",
    "completed",
    "cancelled",
)

ORDER_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "created": frozenset({"pending_payment", "cancelled"}),
    "pending_payment": frozenset({"paid", "cancelled"}),
    "paid": frozenset({"checked_in", "cancelled"}),
    "checked_in": frozenset({"checkout_pending"}),
    "checkout_pending": frozenset({"completed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

ORDER_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "cancelled"})

ORDER_STAY_STATUSES: frozenset[str] = frozenset({"paid", "checked_in", "checkout_pending"})
"""住宿存续（已支付 → 退房完成前）：凭证签发与每日轮换跳过集共用的唯一口径。"""

HOUSEKEEPING_STATUSES: tuple[str, ...] = (
    "pending",
    "accepted",
    "in_progress",
    "done",
    "verified",
    "closed",
    "escalated",
)

HOUSEKEEPING_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"accepted", "escalated"}),
    "accepted": frozenset({"in_progress", "escalated"}),
    "in_progress": frozenset({"done", "escalated"}),
    "done": frozenset({"verified", "escalated"}),
    "verified": frozenset({"closed"}),
    "escalated": frozenset({"in_progress", "closed"}),
    "closed": frozenset(),
}

HOUSEKEEPING_TERMINAL_STATUSES: frozenset[str] = frozenset({"closed"})

HOUSEKEEPING_TASK_TYPES: frozenset[str] = frozenset({"checkout_clean", "call_clean"})

# 后台任务状态机（机制基线 §2.3）：意图落库后由 worker 抢占执行，失联统一失败。
TASK_STATUSES: tuple[str, ...] = ("pending", "running", "succeeded", "failed", "cancelled")

TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

TASK_TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

# 数据库 claim 上限：同一任务行最多被抢占 4 次（重复投递不能无限重试）
TASK_MAX_CLAIMS = 4

KB_DOCUMENT_STATUSES: tuple[str, ...] = (
    "uploaded",
    "parsing",
    "parsed",
    "indexing",
    "indexed",
    "error_parsing",
    "error_indexing",
)

KB_DOCUMENT_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "uploaded": frozenset({"parsing"}),
    "parsing": frozenset({"parsed", "error_parsing"}),
    "parsed": frozenset({"indexing"}),
    "indexing": frozenset({"indexed", "error_indexing"}),
    "indexed": frozenset({"parsing"}),
    "error_parsing": frozenset({"parsing"}),
    "error_indexing": frozenset({"indexing", "parsing"}),
}

# parsing / indexing 为在途状态：并发重复任务不得抢占（条件抢占，只能抢候选集合内的行）
KB_DOCUMENT_ACTIVE_STATUSES: frozenset[str] = frozenset({"parsing", "indexing"})
KB_PARSE_CLAIM_STATUSES: frozenset[str] = frozenset({"uploaded", "error_parsing", "indexed"})
KB_INDEX_CLAIM_STATUSES: frozenset[str] = frozenset({"parsed", "error_indexing"})

# 退房清洁在 verified 步骤必须确认已完成密码修改（A1 负例：未确认不得 closed）
VERIFIED_EVIDENCE_FLAG = "password_rotated"
VERIFIED_EVIDENCE_TASK_TYPES: frozenset[str] = frozenset({"checkout_clean"})

# 状态 → 落库时间戳列：迁移成功时自动写入
ORDER_TIMESTAMP_COLUMNS: Mapping[str, str] = {
    "paid": "paid_at",
    "checked_in": "checked_in_at",
    "completed": "completed_at",
    "cancelled": "cancelled_at",
}

HOUSEKEEPING_TIMESTAMP_COLUMNS: Mapping[str, str] = {
    "accepted": "accepted_at",
    "in_progress": "started_at",
    "done": "done_at",
    "verified": "verified_at",
    "closed": "closed_at",
    "escalated": "escalated_at",
}


class TransitionError(ValueError):
    """状态迁移错误基类：服务层映射为业务错误，绝不静默丢弃。"""


class IllegalTransitionError(TransitionError):
    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法状态迁移：{current} → {target}")
        self.current = current
        self.target = target


class VerificationRequiredError(TransitionError):
    def __init__(self, task_type: str) -> None:
        super().__init__(f"{task_type} 任务进入 verified 前必须确认密码已修改")
        self.task_type = task_type


def order_allows(current: str, target: str) -> bool:
    return target in ORDER_TRANSITIONS.get(current, frozenset())


def housekeeping_allows(current: str, target: str) -> bool:
    return target in HOUSEKEEPING_TRANSITIONS.get(current, frozenset())


def ensure_order_transition(current: str, target: str) -> None:
    if not order_allows(current, target):
        raise IllegalTransitionError(current, target)


def ensure_housekeeping_transition(current: str, target: str) -> None:
    if not housekeeping_allows(current, target):
        raise IllegalTransitionError(current, target)


def kb_document_allows(current: str, target: str) -> bool:
    return target in KB_DOCUMENT_TRANSITIONS.get(current, frozenset())


def ensure_kb_document_transition(current: str, target: str) -> None:
    if not kb_document_allows(current, target):
        raise IllegalTransitionError(current, target)


def kb_document_is_active(status: str) -> bool:
    return status in KB_DOCUMENT_ACTIVE_STATUSES


def task_allows(current: str, target: str) -> bool:
    return target in TASK_TRANSITIONS.get(current, frozenset())


def ensure_task_transition(current: str, target: str) -> None:
    if not task_allows(current, target):
        raise IllegalTransitionError(current, target)


def task_is_terminal(status: str) -> bool:
    return status in TASK_TERMINAL_STATUSES


def ensure_verification_evidence(task_type: str, evidence: Mapping[str, Any] | None) -> None:
    """退房清洁进入 verified 前，留证必须显式确认密码已修改。"""
    if task_type not in VERIFIED_EVIDENCE_TASK_TYPES:
        return
    if not evidence or evidence.get(VERIFIED_EVIDENCE_FLAG) is not True:
        raise VerificationRequiredError(task_type)


def order_is_terminal(status: str) -> bool:
    return status in ORDER_TERMINAL_STATUSES


def housekeeping_is_terminal(status: str) -> bool:
    return status in HOUSEKEEPING_TERMINAL_STATUSES
