"""默认 Agent 评测集真实回归（61 条）与报告落库。

`pytest -m eval` 的验收路径：七份出厂数据集全部走真实模型与生产同源执行链路，
逐集聚合后写入 `agent_eval_reports`，再从库里回读核对指标、指纹与逐题明细。

前置（评测依赖真实外部状态，不在用例内伪造）：
- `MODEL_API_KEY` / `MODEL_NAME` / `MODEL_BASE_URL` 已配置；
- 开发库已迁移并执行过 `python -m app.seed`（房型/房间/员工）；
- 知识库已导入（query_kb 用例）、设备模拟器在线（门锁密码用例）。

本用例会向开发库写入 eval-* 客人的业务副作用（订单/任务/工单）与评测报告；
执行器在每条用例开跑前释放旧评测订单，因此可重复执行。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.eval.agent.dataset import dataset_fingerprint, load_eval_set
from app.eval.agent.executor import RealCaseExecutor
from app.eval.agent.runner import DEFAULT_DATASET_DIR, persist_report, run_eval
from app.repositories.eval_agent_repo import AgentEvalReportRepository
from tests.integration.db_utils import run_with_session_factory

pytestmark = [pytest.mark.integration, pytest.mark.eval]

SessionFactoryArg = async_sessionmaker[AsyncSession]

DATASET_PATHS = tuple(sorted(DEFAULT_DATASET_DIR.glob("*.yaml")))
EXPECTED_DATASETS = 7
EXPECTED_CASES = 61

_SETTINGS = get_settings()
HAS_MODEL_KEY = bool(_SETTINGS.model_api_key)


@pytest.mark.skipif(not HAS_MODEL_KEY, reason="未配置模型密钥：真实评测按约定跳过")
def test_default_eval_sets_run_for_real_and_persist() -> None:
    assert len(DATASET_PATHS) == EXPECTED_DATASETS

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        executor = RealCaseExecutor(checkpointer=InMemorySaver(), session_factory=factory)
        reports: list[tuple[Path, Any, Any, Any]] = []
        async with factory() as session:
            repository = AgentEvalReportRepository(session)
            for path in DATASET_PATHS:
                report = await run_eval(session, dataset_path=path, executor=executor)
                persisted = await persist_report(session, report)
                row = await repository.get(persisted.report_id or 0)
                reports.append((path, report, persisted, row))
        return {"reports": reports}

    reports = run_with_session_factory(_SETTINGS.database_url, _body)["reports"]

    total_cases = sum(report.total_cases for _, report, _, _ in reports)
    passed = sum(int(report.metrics["total_passed"]) for _, report, _, _ in reports)
    assert total_cases == EXPECTED_CASES
    assert passed == total_cases, _failure_detail(reports)

    fingerprints: set[str] = set()
    for path, report, persisted, row in reports:
        assert report.dataset_fingerprint == dataset_fingerprint(load_eval_set(path))
        assert persisted.report_id is not None
        assert row is not None
        assert row.dataset_path == str(path)
        assert row.dataset_fingerprint == report.dataset_fingerprint
        assert row.config_fingerprint == report.config_fingerprint
        assert row.model == report.model == _SETTINGS.model_name
        assert row.total_cases == report.total_cases
        assert row.metrics == report.metrics
        assert row.outcomes == report.outcomes
        fingerprints.add(report.dataset_fingerprint)
    assert len(fingerprints) == EXPECTED_DATASETS


def _failure_detail(reports: list[tuple[Path, Any, Any, Any]]) -> str:
    lines: list[str] = []
    for path, report, _, _ in reports:
        for outcome in report.outcomes:
            expected_success = outcome["expected_success"]
            failed = outcome["tool_accuracy"] < 1.0 or (
                expected_success is not None and outcome["task_success"] != expected_success
            )
            if not failed:
                continue
            expected = [call["name"] for call in outcome["expected_sequence"]]
            actual = [call["name"] for call in outcome["actual_sequence"]]
            lines.append(
                f"{path.name}:{outcome['item_id']} expected={expected} actual={actual} "
                f"success={outcome['task_success']}/{expected_success}"
            )
    return "评测未全量通过：\n" + "\n".join(lines)
