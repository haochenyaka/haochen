"""数据管道处理器单测（PR-D1）：checkpoint scope 与运行终态写入的回归。

这些用例以替身 session/ctx 驱动，不依赖真实 PostgreSQL；集成侧的端到端见
`tests/integration/test_data_pipeline_api.py`。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services.task_registry import TaskContext


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _FakeSession:
    """记录执行的 SQL，用于断言 checkpoint 查询的 scope 条件。"""

    def __init__(self, row: Any = None) -> None:
        self.executed: list[Any] = []
        self._row = row
        self.commits = 0

    async def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.executed.append(stmt)
        return _FakeResult(self._row)

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self) -> _FakeSession:
        return self._session


def _ctx(session: _FakeSession, **payload: Any) -> TaskContext:
    return TaskContext(
        task_id=uuid4(),
        task_type="data_pipeline.export_messages",
        handler_version=1,
        payload={"pipeline_run_id": str(uuid4()), **payload},
        attempt_no=1,
        owner_token="test",
        session_factory=_FakeFactory(session),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_get_checkpoint_reads_by_scope_not_source_table() -> None:
    """回归 bug 1：checkpoint 按 scope 命中 summary，而非用 task_type 查 source_table。"""
    from app.data_pipeline import task_handlers

    session = _FakeSession(row={"checkpoint": 42, "_scope": "data_pipeline.export_messages"})
    ctx = _ctx(session)

    checkpoint = await task_handlers._get_checkpoint(ctx, "data_pipeline.export_messages")

    assert checkpoint == 42
    # 查询必须基于 summary 的 scope 段，而非 source_table（回归 bug 1）
    assert session.executed, "应执行一次查询"
    sql_text = str(session.executed[0])
    assert "source_table" not in sql_text
    assert "summary" in sql_text


@pytest.mark.asyncio
async def test_get_checkpoint_returns_none_when_absent() -> None:
    from app.data_pipeline import task_handlers

    session = _FakeSession(row=None)
    ctx = _ctx(session)
    assert await task_handlers._get_checkpoint(ctx, "data_pipeline.export_messages") is None


@pytest.mark.asyncio
async def test_get_checkpoints_reads_multi_table_map() -> None:
    from app.data_pipeline import task_handlers

    session = _FakeSession(
        row={"checkpoints": {"model_audit": 10, "tool_audit": 20}, "_scope": "x"}
    )
    ctx = _ctx(session)
    assert await task_handlers._get_checkpoints(ctx, "data_pipeline.export_audits") == {
        "model_audit": 10,
        "tool_audit": 20,
    }


@pytest.mark.asyncio
async def test_complete_run_merges_scope_and_checkpoint_into_summary() -> None:
    """_complete_run 应把 checkpoint_scope / checkpoint 并入 summary（写入键统一）。"""
    from app.data_pipeline import task_handlers

    captured: dict[str, Any] = {}

    class _Svc:
        def __init__(self, _session: Any) -> None:
            pass

        async def complete_run(self, run_id: UUID, **kwargs: Any) -> None:
            captured.update(kwargs)

    session = _FakeSession()
    ctx = _ctx(session)

    import app.services.data_pipeline_service as svc_mod

    original = svc_mod.DataPipelineService
    svc_mod.DataPipelineService = _Svc  # type: ignore[misc]
    try:
        await task_handlers._complete_run(
            ctx,
            status="completed",
            source_table="messages",
            summary={"exported": 3},
            checkpoint_scope="data_pipeline.export_messages",
            checkpoint=99,
        )
    finally:
        svc_mod.DataPipelineService = original  # type: ignore[misc]

    assert captured["status"] == "completed"
    assert captured["summary"]["_scope"] == "data_pipeline.export_messages"
    assert captured["summary"]["checkpoint"] == 99
    assert captured["summary"]["exported"] == 3
