"""智能体评估集成测试（真实 PostgreSQL）：报告落库与基线作废。

覆盖链路：run_eval → persist_report（报告追加 + 同数据集旧报告按指纹作废）。
评估执行器使用 ScriptedCaseExecutor 替代真实 Agent Graph。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.toolkits import ToolRegistry, builtin_tool
from app.config import get_settings
from app.eval.agent.dataset import dataset_fingerprint
from app.eval.agent.executor import RealCaseExecutor
from app.eval.agent.runner import (
    ScriptedCaseExecutor,
    evaluate_items,
    persist_report,
    run_eval,
)
from app.models.eval_agent import AgentEvalReport
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import ScriptedToolChatModel

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

HAS_MODEL_KEY = bool(get_settings().model_api_key)


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
    """同数据集 + 配置变化：旧报告被标记作废。"""
    from app.eval.agent.dataset import load_eval_set
    from app.eval.agent.runner import config_fingerprint_value

    dataset_path = _write_dataset(tmp_path, "test_set2.yaml")
    executor = ScriptedCaseExecutor(
        tool_calls=[{"name": "test_tool", "args": {}}],
        task_success=True,
    )
    items = load_eval_set(dataset_path)
    base_settings = get_settings()
    # 第二次运行的配置指纹变化（模拟管理端改配置）：同数据集、相同模型
    changed_settings = base_settings.model_copy(update={"model_name": "changed-model"})
    base_cfp = config_fingerprint_value(base_settings, None)
    changed_cfp = config_fingerprint_value(changed_settings, None)
    assert base_cfp != changed_cfp

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            report1 = await evaluate_items(
                items=items,
                executor=executor,
                settings=base_settings,
                dataset_path=str(dataset_path),
            )
            p1 = await persist_report(session, report1)
            report2 = await evaluate_items(
                items=items,
                executor=executor,
                settings=changed_settings,
                dataset_path=str(dataset_path),
            )
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


def _write_case_dataset(
    tmp_path: Path,
    *,
    case_id: str,
    message: str,
    tool_name: str,
    tool_args: dict[str, object],
) -> Path:
    path = tmp_path / f"{case_id}.yaml"
    data = {
        "fingerprint_meta": {"name": case_id, "description": ""},
        "cases": [
            {
                "id": case_id,
                "name": case_id,
                "message": message,
                "expected": {
                    "tool_sequence": [{"name": tool_name, "args_contain": dict(tool_args)}],
                    "task_success": True,
                },
                "tags": ["real"],
            }
        ],
    }
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_real_case_executor_reads_tool_facts(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """真实执行器走生产接入路径跑真实编译图：工具序列从 tool_audit 回读。

    模型用确定性替身（不依赖外部模型与网络）：本用例验证执行器链路——接入 → 队头 Run →
    真实 supervisor 图 → 工具事实 → 审计回读 → 打分落库；真实模型的评测见下方用例。
    """
    settings = get_settings()
    monkeypatch.setattr(settings, "model_api_key", "test-key")
    monkeypatch.setattr(settings, "model_name", "scripted-tool")

    registry = ToolRegistry()

    @builtin_tool(category="test", registry=registry)
    async def test_echo(text: str) -> str:
        """回显文本。"""
        return f"echo:{text}"

    model = ScriptedToolChatModel(tool_name="test_echo", tool_args={"text": "你好"})
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )
    monkeypatch.setattr(FrontDeskAgent, "tool_ids", ("test_echo",))

    dataset_path = _write_case_dataset(
        tmp_path,
        case_id="real-001",
        message="现在几点？",
        tool_name="test_echo",
        tool_args={"text": "你好"},
    )

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        executor = RealCaseExecutor(
            checkpointer=InMemorySaver(),
            resource_policy=StaticResourcePolicy(allowed=frozenset({("tool", "test_echo")})),
            tool_registry=registry,
            session_factory=factory,
        )
        async with factory() as session:
            report = await run_eval(session, dataset_path=dataset_path, executor=executor)
            persisted = await persist_report(session, report)
        return {"report": report, "persisted": persisted}

    values = run_with_session_factory(runtime_db_url, _body)
    report = values["report"]
    assert values["persisted"].report_id is not None
    assert report.total_cases == 1
    outcome = report.outcomes[0]
    assert [call["name"] for call in outcome["actual_sequence"]] == ["test_echo"]
    assert outcome["tool_accuracy"] == 1.0
    assert outcome["args_match_rate"] == 1.0
    assert outcome["task_success"] is True
    assert report.metrics["total_passed"] == 1


@pytest.mark.eval
@pytest.mark.skipif(not HAS_MODEL_KEY, reason="未配置模型密钥：真实评测按约定跳过")
def test_real_case_executor_against_real_model(runtime_db_url: str, tmp_path: Path) -> None:
    """真实模型下的评测真跑：只用只读工具，不产生订单/任务等业务副作用。"""
    dataset_path = tmp_path / "real_model.yaml"
    dataset_path.write_text(
        yaml.dump(
            {
                "fingerprint_meta": {"name": "real-model", "description": ""},
                "cases": [
                    {
                        "id": "real-consult-001",
                        "name": "房型咨询",
                        "message": "你们有哪些房型？",
                        "expected": {
                            "tool_sequence": [{"name": "list_room_types"}],
                            "task_success": True,
                        },
                        "tags": ["consult"],
                    },
                    {
                        "id": "real-consult-002",
                        "name": "时间咨询",
                        "message": "现在几点了？",
                        "expected": {
                            "tool_sequence": [{"name": "get_current_time"}],
                            "task_success": True,
                        },
                        "tags": ["consult"],
                    },
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    async def _body(factory: SessionFactoryArg) -> Any:
        from langgraph.checkpoint.memory import InMemorySaver

        executor = RealCaseExecutor(checkpointer=InMemorySaver(), session_factory=factory)
        async with factory() as session:
            return await run_eval(session, dataset_path=dataset_path, executor=executor)

    report = run_with_session_factory(runtime_db_url, _body)
    assert report.total_cases == 2
    assert all("actual_sequence" in outcome for outcome in report.outcomes)
    called = sum(len(outcome["actual_sequence"]) for outcome in report.outcomes)
    assert called >= 1, "真实模型未调用任何工具，评测链路未真正跑通"
