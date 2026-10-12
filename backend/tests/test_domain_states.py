"""状态机单元测试：合法迁移矩阵穷举、留证门槛与词汇一致性（术语表 §2/§4）。"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.domain.states import (
    HOUSEKEEPING_STATUSES,
    HOUSEKEEPING_TASK_TYPES,
    HOUSEKEEPING_TIMESTAMP_COLUMNS,
    HOUSEKEEPING_TRANSITIONS,
    KB_DOCUMENT_ACTIVE_STATUSES,
    KB_DOCUMENT_STATUSES,
    KB_DOCUMENT_TRANSITIONS,
    KB_INDEX_CLAIM_STATUSES,
    KB_PARSE_CLAIM_STATUSES,
    ORDER_STATUSES,
    ORDER_TIMESTAMP_COLUMNS,
    ORDER_TRANSITIONS,
    IllegalTransitionError,
    VerificationRequiredError,
    ensure_housekeeping_transition,
    ensure_kb_document_transition,
    ensure_order_transition,
    ensure_verification_evidence,
    housekeeping_allows,
    housekeeping_is_terminal,
    kb_document_allows,
    kb_document_is_active,
    order_allows,
    order_is_terminal,
)


def test_order_transition_matrix_exhaustive() -> None:
    for current in ORDER_STATUSES:
        for target in ORDER_STATUSES:
            allowed = target in ORDER_TRANSITIONS[current]
            assert order_allows(current, target) is allowed
            if allowed:
                ensure_order_transition(current, target)
            else:
                with pytest.raises(IllegalTransitionError):
                    ensure_order_transition(current, target)


def test_housekeeping_transition_matrix_exhaustive() -> None:
    for current in HOUSEKEEPING_STATUSES:
        for target in HOUSEKEEPING_STATUSES:
            allowed = target in HOUSEKEEPING_TRANSITIONS[current]
            assert housekeeping_allows(current, target) is allowed
            if allowed:
                ensure_housekeeping_transition(current, target)
            else:
                with pytest.raises(IllegalTransitionError):
                    ensure_housekeeping_transition(current, target)


def test_transition_maps_cover_vocabulary() -> None:
    assert set(ORDER_TRANSITIONS) == set(ORDER_STATUSES)
    assert set(HOUSEKEEPING_TRANSITIONS) == set(HOUSEKEEPING_STATUSES)
    for targets in ORDER_TRANSITIONS.values():
        assert targets <= set(ORDER_STATUSES)
    for targets in HOUSEKEEPING_TRANSITIONS.values():
        assert targets <= set(HOUSEKEEPING_STATUSES)
    assert set(ORDER_TIMESTAMP_COLUMNS) <= set(ORDER_STATUSES)
    assert set(HOUSEKEEPING_TIMESTAMP_COLUMNS) <= set(HOUSEKEEPING_STATUSES)


def test_order_happy_chain_and_cancel_branches() -> None:
    current = "created"
    for target in ("pending_payment", "paid", "checked_in", "checkout_pending", "completed"):
        ensure_order_transition(current, target)
        current = target

    for source in ("created", "pending_payment", "paid"):
        ensure_order_transition(source, "cancelled")
    for source in ("checked_in", "checkout_pending"):
        with pytest.raises(IllegalTransitionError):
            ensure_order_transition(source, "cancelled")


def test_order_terminal_statuses_have_no_successors() -> None:
    assert ORDER_TRANSITIONS["completed"] == frozenset()
    assert ORDER_TRANSITIONS["cancelled"] == frozenset()
    assert order_is_terminal("completed") and order_is_terminal("cancelled")
    assert not order_is_terminal("checkout_pending")


def test_every_non_terminal_order_status_reaches_completed() -> None:
    for status in ORDER_STATUSES:
        if not order_is_terminal(status):
            assert "completed" in _reachable(ORDER_TRANSITIONS, status), status


def test_housekeeping_happy_chain_and_escalation() -> None:
    current = "pending"
    for target in ("accepted", "in_progress", "done", "verified", "closed"):
        ensure_housekeeping_transition(current, target)
        current = target

    ensure_housekeeping_transition("pending", "escalated")
    ensure_housekeeping_transition("accepted", "escalated")
    ensure_housekeeping_transition("in_progress", "escalated")
    ensure_housekeeping_transition("done", "escalated")
    ensure_housekeeping_transition("escalated", "in_progress")
    ensure_housekeeping_transition("escalated", "closed")
    for target in ("accepted", "done", "verified"):
        with pytest.raises(IllegalTransitionError):
            ensure_housekeeping_transition("escalated", target)


def test_every_non_terminal_housekeeping_status_reaches_closed() -> None:
    for status in HOUSEKEEPING_STATUSES:
        if not housekeeping_is_terminal(status):
            assert "closed" in _reachable(HOUSEKEEPING_TRANSITIONS, status), status


def test_verification_evidence_gate() -> None:
    ensure_verification_evidence("checkout_clean", {"password_rotated": True})
    ensure_verification_evidence("call_clean", None)
    ensure_verification_evidence("call_clean", {"photo_url": "s3://x/1.jpg"})

    for evidence in (None, {}, {"photo_url": "s3://x/1.jpg"}, {"password_rotated": False}):
        with pytest.raises(VerificationRequiredError):
            ensure_verification_evidence("checkout_clean", evidence)


def test_housekeeping_task_type_vocabulary() -> None:
    assert HOUSEKEEPING_TASK_TYPES == {"checkout_clean", "call_clean"}


def test_kb_document_transition_matrix_exhaustive() -> None:
    for current in KB_DOCUMENT_STATUSES:
        for target in KB_DOCUMENT_STATUSES:
            allowed = target in KB_DOCUMENT_TRANSITIONS[current]
            assert kb_document_allows(current, target) is allowed
            if allowed:
                ensure_kb_document_transition(current, target)
            else:
                with pytest.raises(IllegalTransitionError):
                    ensure_kb_document_transition(current, target)


def test_kb_transition_map_covers_vocabulary() -> None:
    assert set(KB_DOCUMENT_TRANSITIONS) == set(KB_DOCUMENT_STATUSES)
    for targets in KB_DOCUMENT_TRANSITIONS.values():
        assert targets <= set(KB_DOCUMENT_STATUSES)


def test_kb_happy_chain_and_retry_branches() -> None:
    current = "uploaded"
    for target in ("parsing", "parsed", "indexing", "indexed"):
        ensure_kb_document_transition(current, target)
        current = target

    # 重传/重建索引：indexed 可回到 parsing；错误态可重试
    ensure_kb_document_transition("indexed", "parsing")
    ensure_kb_document_transition("error_parsing", "parsing")
    ensure_kb_document_transition("error_indexing", "indexing")
    ensure_kb_document_transition("error_indexing", "parsing")

    for source, target in (
        ("uploaded", "indexed"),
        ("uploaded", "parsed"),
        ("parsing", "parsing"),
        ("parsed", "parsed"),
        ("indexed", "indexed"),
        ("error_parsing", "indexed"),
        ("error_indexing", "parsed"),
    ):
        with pytest.raises(IllegalTransitionError):
            ensure_kb_document_transition(source, target)


def test_kb_active_statuses_and_claim_candidates() -> None:
    assert KB_DOCUMENT_ACTIVE_STATUSES == {"parsing", "indexing"}
    assert KB_PARSE_CLAIM_STATUSES == {"uploaded", "error_parsing", "indexed"}
    assert KB_INDEX_CLAIM_STATUSES == {"parsed", "error_indexing"}
    # 抢占互斥：在途状态不可能是任何抢占的候选，保证并发不双写
    assert not (KB_DOCUMENT_ACTIVE_STATUSES & KB_PARSE_CLAIM_STATUSES)
    assert not (KB_DOCUMENT_ACTIVE_STATUSES & KB_INDEX_CLAIM_STATUSES)
    for status in KB_DOCUMENT_STATUSES:
        assert kb_document_is_active(status) is (status in KB_DOCUMENT_ACTIVE_STATUSES)


def test_kb_every_status_reaches_indexed_or_is_recoverable() -> None:
    for status in KB_DOCUMENT_STATUSES:
        assert "parsing" in _reachable(KB_DOCUMENT_TRANSITIONS, status), status


def _reachable(transitions: Mapping[str, frozenset[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        for nxt in transitions[node]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen
