"""数据管道调度器单测（PR-D3）：cron 命中判定与分钟级去重键。"""

from __future__ import annotations

from datetime import UTC, datetime

from app.data_pipeline.scheduler import is_due, schedule_dedupe_key


def _at(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


def test_is_due_every_minute() -> None:
    assert is_due("* * * * *", _at(2026, 10, 18, 3, 7)) is True


def test_is_due_specific_time() -> None:
    assert is_due("0 3 * * *", _at(2026, 10, 18, 3, 0)) is True
    assert is_due("0 3 * * *", _at(2026, 10, 18, 3, 1)) is False
    assert is_due("0 3 * * *", _at(2026, 10, 18, 4, 0)) is False


def test_is_due_step_cron() -> None:
    assert is_due("*/30 * * * *", _at(2026, 10, 18, 5, 0)) is True
    assert is_due("*/30 * * * *", _at(2026, 10, 18, 5, 30)) is True
    assert is_due("*/30 * * * *", _at(2026, 10, 18, 5, 15)) is False


def test_is_due_invalid_cron_does_not_raise() -> None:
    assert is_due("not a cron", _at(2026, 10, 18, 3, 0)) is False


def test_schedule_dedupe_key_is_minute_scoped() -> None:
    a = schedule_dedupe_key(1, _at(2026, 10, 18, 3, 0, 5))
    b = schedule_dedupe_key(1, _at(2026, 10, 18, 3, 0, 55))
    c = schedule_dedupe_key(1, _at(2026, 10, 18, 3, 1, 0))
    d = schedule_dedupe_key(2, _at(2026, 10, 18, 3, 0, 0))
    assert a == b, "同一分钟内（不同秒）应同键"
    assert a != c, "跨分钟应不同键"
    assert a != d, "不同规则应不同键"