"""智能体评估单元测试：数据集校验、指标口径、指纹与运行器。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.eval.agent.dataset import (
    AgentEvalItem,
    EvalSetError,
    ExpectedSpec,
    ToolCallSpec,
    dataset_fingerprint,
    fingerprint,
    load_eval_set,
)
from app.eval.agent.metrics import aggregate, score_question
from app.eval.agent.runner import (
    DEFAULT_DATASET_DIR,
    ScriptedCaseExecutor,
    evaluate_items,
)


def _make_item(
    item_id: str = "test-001",
    tool_sequence: list[dict] | None = None,
    task_success: bool | None = True,
) -> AgentEvalItem:
    ts = (
        tool_sequence
        if tool_sequence is not None
        else [{"name": "inquire_room_type", "args_contain": {"room_type": "大床房"}}]
    )
    return AgentEvalItem(
        id=item_id,
        name=item_id,
        message="我要订一间大床房",
        history=(),
        guest_overrides=None,
        expected=ExpectedSpec(
            tool_sequence=[
                ToolCallSpec(name=t["name"], args_contain=t.get("args_contain")) for t in ts
            ],
            task_success=task_success,
            answer_contains=[],
        ),
        tags=("order", "happy-path"),
    )


def _yaml_case(
    case_id: str = "test-001",
    message: str = "测试消息",
    expected_tools: list[dict] | None = None,
) -> dict:
    et = expected_tools or [{"name": "test_tool"}]
    return {
        "id": case_id,
        "name": case_id,
        "message": message,
        "expected": {
            "tool_sequence": et,
            "task_success": True,
        },
        "tags": ["test"],
    }


def _write_dataset(tmp_path: Path, name: str, cases: list[dict]) -> Path:
    path = tmp_path / name
    data = {"fingerprint_meta": {"name": name, "description": ""}, "cases": cases}
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    return path


class TestDataset:
    def test_load_valid(self, tmp_path: Path) -> None:
        path = _write_dataset(tmp_path, "valid.yaml", [_yaml_case()])
        items = load_eval_set(path)
        assert len(items) == 1
        assert items[0].id == "test-001"
        assert items[0].message == "测试消息"

    def test_empty_cases(self, tmp_path: Path) -> None:
        path = _write_dataset(tmp_path, "empty.yaml", [])
        with pytest.raises(EvalSetError, match="非空数组"):
            load_eval_set(path)

    def test_duplicate_id(self, tmp_path: Path) -> None:
        path = _write_dataset(tmp_path, "dup.yaml", [_yaml_case(), _yaml_case(case_id="test-001")])
        with pytest.raises(EvalSetError, match="id 重复"):
            load_eval_set(path)

    def test_missing_message(self, tmp_path: Path) -> None:
        path = _write_dataset(
            tmp_path, "no_msg.yaml", [{"id": "x", "expected": {"tool_sequence": []}}]
        )
        with pytest.raises(EvalSetError, match="message"):
            load_eval_set(path)

    def test_with_history(self, tmp_path: Path) -> None:
        case = _yaml_case()
        case["history"] = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "有什么需要帮助的？"},
        ]
        path = _write_dataset(tmp_path, "hist.yaml", [case])
        items = load_eval_set(path)
        assert len(items[0].history) == 2
        assert items[0].history[0].role == "user"

    def test_with_guest_overrides(self, tmp_path: Path) -> None:
        case = _yaml_case()
        case["guest_overrides"] = {"name": "张三"}
        path = _write_dataset(tmp_path, "override.yaml", [case])
        items = load_eval_set(path)
        assert items[0].guest_overrides == {"name": "张三"}


class TestFingerprint:
    def test_stability(self) -> None:
        items = [_make_item("a"), _make_item("b")]
        fp1 = dataset_fingerprint(items)
        fp2 = dataset_fingerprint(items)
        assert fp1 == fp2

    def test_content_sensitive(self) -> None:
        items_a = [_make_item("a")]
        items_b = [_make_item("a", task_success=False)]
        assert dataset_fingerprint(items_a) != dataset_fingerprint(items_b)

    def test_order_sensitive(self) -> None:
        items = [_make_item("a"), _make_item("b")]
        fp1 = dataset_fingerprint(items)
        fp2 = dataset_fingerprint(list(reversed(items)))
        assert fp1 != fp2

    def test_fingerprint_deterministic(self) -> None:
        assert fingerprint({"a": 1}) == fingerprint({"a": 1})


class TestMetrics:
    def test_tool_accuracy_exact_match(self) -> None:
        item = _make_item(tool_sequence=[{"name": "tool_a"}, {"name": "tool_b"}])
        actual = [{"name": "tool_a", "args": {}}, {"name": "tool_b", "args": {}}]
        outcome = score_question(item, actual)
        assert outcome.tool_accuracy == 1.0

    def test_tool_accuracy_partial(self) -> None:
        item = _make_item(tool_sequence=[{"name": "tool_a"}, {"name": "tool_b"}])
        actual = [{"name": "tool_a", "args": {}}]
        outcome = score_question(item, actual)
        assert outcome.tool_accuracy == 0.5

    def test_tool_accuracy_no_match(self) -> None:
        item = _make_item(tool_sequence=[{"name": "tool_a"}])
        actual = [{"name": "wrong_tool", "args": {}}]
        outcome = score_question(item, actual)
        assert outcome.tool_accuracy == 0.0

    def test_tool_accuracy_empty_expected(self) -> None:
        item = _make_item(tool_sequence=[])
        actual = [{"name": "tool_a", "args": {}}]
        outcome = score_question(item, actual)
        assert outcome.tool_accuracy == 1.0  # empty expectation = always pass

    def test_args_match(self) -> None:
        item = _make_item(
            tool_sequence=[{"name": "inquire_room_type", "args_contain": {"room_type": "大床房"}}]
        )
        actual = [{"name": "inquire_room_type", "args": {"room_type": "大床房", "extra": True}}]
        outcome = score_question(item, actual)
        assert outcome.args_match_rate == 1.0

    def test_args_mismatch(self) -> None:
        item = _make_item(
            tool_sequence=[{"name": "inquire_room_type", "args_contain": {"room_type": "大床房"}}]
        )
        actual = [{"name": "inquire_room_type", "args": {"room_type": "标准间"}}]
        outcome = score_question(item, actual)
        assert outcome.args_match_rate == 0.0

    def test_task_success_tracking(self) -> None:
        item = _make_item(task_success=True)
        outcome = score_question(item, [{"name": "tool_a", "args": {}}], task_success=True)
        assert outcome.task_success is True
        assert outcome.expected_success is True

    def test_aggregate_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="为空"):
            aggregate([])

    def test_aggregate_all_pass(self) -> None:
        items = [
            _make_item("a", tool_sequence=[{"name": "tool_a"}]),
            _make_item("b", tool_sequence=[{"name": "tool_a"}]),
        ]
        outcomes = [
            score_question(items[0], [{"name": "tool_a", "args": {}}], task_success=True),
            score_question(items[1], [{"name": "tool_a", "args": {}}], task_success=True),
        ]
        metrics = aggregate(outcomes)
        assert metrics.total_passed == 2
        assert metrics.avg_tool_accuracy == 1.0
        assert metrics.task_success_rate == 1.0

    def test_aggregate_by_tag(self) -> None:
        item_a = AgentEvalItem(
            id="a",
            name="a",
            message="m",
            history=(),
            guest_overrides=None,
            expected=ExpectedSpec(
                tool_sequence=[ToolCallSpec(name="t")], task_success=True, answer_contains=[]
            ),
            tags=("order",),
        )
        item_b = AgentEvalItem(
            id="b",
            name="b",
            message="m",
            history=(),
            guest_overrides=None,
            expected=ExpectedSpec(
                tool_sequence=[ToolCallSpec(name="t")], task_success=True, answer_contains=[]
            ),
            tags=("checkin",),
        )
        outcomes = [
            score_question(item_a, [{"name": "t", "args": {}}], task_success=True),
            score_question(item_b, [{"name": "t", "args": {}}], task_success=True),
        ]
        metrics = aggregate(outcomes)
        assert "order" in metrics.by_tag
        assert "checkin" in metrics.by_tag
        assert metrics.by_tag["order"]["pass_rate"] == 1.0

    def test_outcome_as_dict(self) -> None:
        item = _make_item(tool_sequence=[{"name": "tool_a"}])
        outcome = score_question(item, [{"name": "tool_a", "args": {}}], task_success=True)
        d = outcome.as_dict()
        assert d["tool_accuracy"] == 1.0
        assert d["task_success"] is True
        assert d["item_id"] == "test-001"


class TestRunner:
    @pytest.mark.anyio
    async def test_evaluate_items_with_scripted_executor(self) -> None:
        items = [_make_item("a"), _make_item("b")]
        executor = ScriptedCaseExecutor(
            tool_calls=[{"name": "inquire_room_type", "args": {"room_type": "大床房"}}],
            task_success=True,
        )
        report = await evaluate_items(items=items, executor=executor)
        assert report.total_cases == 2
        assert report.metrics["total_passed"] == 2
        assert report.metrics["avg_tool_accuracy"] == 1.0

    @pytest.mark.anyio
    async def test_evaluate_items_missing_tool(self) -> None:
        items = [_make_item("a", tool_sequence=[{"name": "expected_tool"}])]
        executor = ScriptedCaseExecutor(tool_calls=[], task_success=False)
        report = await evaluate_items(items=items, executor=executor)
        assert report.metrics["total_passed"] == 0
        assert report.metrics["avg_tool_accuracy"] == 0.0

    def test_default_datasets_exist(self) -> None:
        """出厂数据集存在且可加载。"""
        assert DEFAULT_DATASET_DIR.is_dir()
        yaml_files = list(DEFAULT_DATASET_DIR.glob("*.yaml"))
        assert len(yaml_files) >= 3  # orders, checkin, dispatch
        for fpath in yaml_files:
            items = load_eval_set(fpath)
            assert len(items) >= 1
