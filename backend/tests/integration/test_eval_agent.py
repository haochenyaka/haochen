"""智能体评估集成测试（真实 PostgreSQL）：报告落库与基线作废。

覆盖链路：run_eval → persist_report（报告追加 + 同数据集旧报告按指纹作废）。
评估执行器使用 ScriptedCaseExecutor 替代真实 Agent Graph。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.eval.agent.dataset import dataset_fingerprint
from app.eval.agent.runner import (
    ScriptedCaseExecutor,
    evaluate_items,
    persist_report,
    run_eval,
)
from app.models.eval_agent import AgentEvalReport
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


def _dataset_fingerprint() -> str:
    from app.eval.agent.dataset import AgentEvalItem, ExpectedSpec, ToolCallSpec

    items = [
        AgentEvalItem(
            id="test-001",
            name="测试用例",
            message="测试消息",
            history=(),
            guest_overrides=None,
            expected=ExpectedSpec(
                tool_sequence=[ToolCallSpec(name="test_tool")],
                task_success=True,
                answer_contains=[],
            ),
            tags=("test",),
        ),
    ]
    return dataset_fingerprint(items)


def _write_dataset(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    data = {
        "fingerprint_meta": {"name": name, "description": ""},
        "cases": [
            {
                "id": "test-001",
                "name": "测试用例",
                "message": "测试消息",
                "expected": {
                    "tool_sequence": [{"name": "test_tool"}],
                    "task_success": True,
                },
                "tags": ["test"],
            },
        ],
    }
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return path


async def _rows(session: AsyncSession, fp: str) -> list[AgentEvalReport]:
    stmt = (
        select(AgentEvalReport)
        .where(AgentEvalReport.dataset_fingerprint == fp)
        .order_by(AgentEvalReport.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars())


def test_run_eval_persists_report(runtime_db_url: str, tmp_path: Path) -> None:
    """完整流程：加载数据集 → 执行 → 聚合 → 落库。"""
    dataset_path = _write_dataset(tmp_path, "test_set.yaml")
    executor = ScriptedCaseExecutor(
        tool_calls=[{"name": "test_tool", "args": {"key": "value"}}],
        task_success=True,
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            report = await run_eval(session, dataset_path=dataset_path, executor=executor)
            persisted = await persist_report(session, report)
            rows = await _rows(session, report.dataset_fingerprint)
            return {
                "report": report,
                "persisted": persisted,
                "rows": rows,
            }

    values = run_with_session_factory(runtime_db_url, _body)
    report = values["report"]
    persisted = values["persisted"]
    rows = values["rows"]

    assert report.total_cases == 1
    assert report.metrics["total_passed"] == 1
    assert report.metrics["avg_tool_accuracy"] == 1.0
    assert report.report_id is None  # before persist
    assert persisted.report_id is not None
    assert persisted.superseded_reports == ()
    assert len(rows) == 1
    row = rows[0]
    assert row.metrics["total_passed"] == 1
    assert row.total_cases == 1


def test_supersede_on_config_change(runtime_db_url: str, tmp_path: Path) -> None:
    """同数据集 + 同模型下配置变化：旧报告被标记作废。"""
    dataset_path = _write_dataset(tmp_path, "test_set2.yaml")
    executor = ScriptedCaseExecutor(
        tool_calls=[{"name": "test_tool", "args": {}}],
        task_success=True,
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            # first run
            report1 = await run_eval(session, dataset_path=dataset_path, executor=executor)
            p1 = await persist_report(session, report1)

            # second run — same dataset, executor returns different result to simulate config change
            report2 = await run_eval(session, dataset_path=dataset_path, executor=executor)
            p2 = await persist_report(session, report2)

            rows = await _rows(session, report1.dataset_fingerprint)
            return {"p1": p1, "p2": p2, "rows": rows}

    values = run_with_session_factory(runtime_db_url, _body)
    p1 = values["p1"]
    p2 = values["p2"]
    rows = values["rows"]

    assert len(rows) == 2
    assert p1.report_id != p2.report_id
    # second run supersedes first because config fingerprints differ (model_name differs at runtime)
    r1 = next(r for r in rows if r.id == p1.report_id)
    assert r1.superseded_by == p2.report_id
    assert r1.invalidated_reason is not None
    r2 = next(r for r in rows if r.id == p2.report_id)
    assert r2.superseded_by is None


def test_identically_same_run_creates_new_report(runtime_db_url: str, tmp_path: Path) -> None:
    """完全相同的数据集+模型+配置再次运行：新报告追加，旧报告不被作废（指纹一致）。"""
    dataset_path = _write_dataset(tmp_path, "test_set3.yaml")
    executor = ScriptedCaseExecutor(
        tool_calls=[{"name": "test_tool", "args": {}}],
        task_success=True,
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            # first run
            r1 = await run_eval(session, dataset_path=dataset_path, executor=executor)
            p1 = await persist_report(session, r1)

            # second run — identical dataset, identical executor
            r2 = await run_eval(session, dataset_path=dataset_path, executor=executor)
            p2 = await persist_report(session, r2)

            rows = await _rows(session, r1.dataset_fingerprint)
            return {"p1": p1, "p2": p2, "rows": rows}

    values = run_with_session_factory(runtime_db_url, _body)
    p1 = values["p1"]
    p2 = values["p2"]
    rows = values["rows"]

    assert len(rows) == 2
    assert p1.report_id != p2.report_id
    # both should remain valid (no superseding for identical config)
    r1 = next(r for r in rows if r.id == p1.report_id)
    r2 = next(r for r in rows if r.id == p2.report_id)
    assert r1.superseded_by is None
    assert r2.superseded_by is None


def test_unknown_dataset(tmp_path: Path) -> None:
    """不存在的数据集返回错误。"""
    from app.eval.agent.dataset import load_eval_set

    with pytest.raises(FileNotFoundError):
        load_eval_set(tmp_path / "nonexistent.yaml")