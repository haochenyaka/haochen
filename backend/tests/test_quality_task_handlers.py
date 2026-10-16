"""qa.audit 跑批任务单元测试：注册解析、版本失败关闭、去重键契约。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.domain.vocabulary import HOTEL_TIMEZONE
from app.quality import task_handlers
from app.services.task_registry import reset_registry, resolve_handler


@pytest.fixture
def fresh_registry() -> Iterator[None]:
    """干净注册表：reset 递增生成期，下一次解析会重放 qa.audit 模块的注册。

    不依赖别的用例是否已导入本模块——`reset_registry` 已能在模块被缓存时重放注册。
    """
    reset_registry()
    yield
    reset_registry()


def test_qa_audit_handler_registered_at_current_version(fresh_registry: None) -> None:
    spec = resolve_handler(task_handlers.QA_AUDIT_TASK_TYPE, task_handlers.QA_AUDIT_HANDLER_VERSION)
    assert spec is not None
    assert spec.key == ("qa.audit", task_handlers.QA_AUDIT_HANDLER_VERSION)
    assert spec.on_failure is None


def test_qa_audit_unknown_version_fails_closed(fresh_registry: None) -> None:
    assert (
        resolve_handler(
            task_handlers.QA_AUDIT_TASK_TYPE, task_handlers.QA_AUDIT_HANDLER_VERSION + 1
        )
        is None
    )


def test_repeated_resolution_reuses_registered_handler() -> None:
    """无 reset 时重复解析复用同一处理器对象（不重放模块）。"""
    spec = resolve_handler(task_handlers.QA_AUDIT_TASK_TYPE, task_handlers.QA_AUDIT_HANDLER_VERSION)
    assert spec is not None
    again = resolve_handler(
        task_handlers.QA_AUDIT_TASK_TYPE, task_handlers.QA_AUDIT_HANDLER_VERSION
    )
    assert again is not None
    assert again.run is spec.run is task_handlers.audit_quality


def test_qa_audit_dedupe_key_is_hour_granular_in_hotel_timezone() -> None:
    moment = datetime(2026, 3, 5, 2, 41, 30, tzinfo=UTC)  # 酒店本地为当日 10:41
    key = task_handlers.qa_audit_dedupe_key(moment)
    local = moment.astimezone(HOTEL_TIMEZONE)
    assert key == f"{task_handlers.QA_AUDIT_DEDUPE_PREFIX}{local.strftime('%Y%m%dT%H')}"
    # 同一小时内的不同时刻（含分钟/秒）落在同一去重键
    assert task_handlers.qa_audit_dedupe_key(moment + timedelta(minutes=18, seconds=12)) == key


def test_qa_audit_dedupe_key_rolls_over_per_hour() -> None:
    moment = datetime(2026, 3, 5, 2, 41, 0, tzinfo=timezone(timedelta(hours=8)))
    later = moment + timedelta(hours=1)
    assert task_handlers.qa_audit_dedupe_key(moment) != task_handlers.qa_audit_dedupe_key(later)
