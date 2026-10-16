"""智能体评估单元测试：数据集校验、指标口径、指纹与运行器。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
import yaml

from app.config import get_settings
from app.eval.agent.dataset import (
    AgentEvalItem,
    EvalSetError,
    ExpectedSpec,
    ToolCallSpec,
    dataset_fingerprint,
    fingerprint,
    load_eval_set,
)
from app.eval.agent.executor import RealCaseExecutor, task_success_from, tool_calls_from_audit
from app.eval.agent.metrics import aggregate, score_question
from app.eval.agent.runner import (
    DEFAULT_DATASET_DIR,
    EvalRunError,
    ScriptedCaseExecutor,
    config_fingerprint_value,
    evaluate_items,
    executor_identity,
)
from app.repositories.audit_repository import AuditEntry


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

    def test_multi_turn_declares_turns_only(self, tmp_path: Path) -> None:
        """多轮用例可只给 turns：message 取末条，供展示与报告。"""
        case = _yaml_case()
        case.pop("message")
        case["turns"] = ["你们有哪些房型？", "就订那间大床房，今晚一晚"]
        path = _write_dataset(tmp_path, "turns.yaml", [case])
        items = load_eval_set(path)
        assert items[0].turns == ("你们有哪些房型？", "就订那间大床房，今晚一晚")
        assert items[0].message == "就订那间大床房，今晚一晚"
        assert items[0].effective_turns() == items[0].turns

    def test_single_turn_effective_turns_is_message(self) -> None:
        item = _make_item()
        assert item.turns == ()
        assert item.effective_turns() == ("我要订一间大床房",)

    def test_message_must_match_last_turn(self, tmp_path: Path) -> None:
        """message 与 turns 末条不一致即报错：两处各写一遍最容易漂移。"""
        case = _yaml_case(message="第三轮")
        case["turns"] = ["第一轮", "第二轮"]
        path = _write_dataset(tmp_path, "mismatch.yaml", [case])
        with pytest.raises(EvalSetError, match="turns 末条"):
            load_eval_set(path)

    def test_message_may_repeat_last_turn(self, tmp_path: Path) -> None:
        case = _yaml_case(message="第二轮")
        case["turns"] = ["第一轮", "第二轮"]
        path = _write_dataset(tmp_path, "match.yaml", [case])
        assert load_eval_set(path)[0].message == "第二轮"

    @pytest.mark.parametrize("bad", [[], "不是数组", ["第一轮", "  "]])
    def test_turns_invalid(self, tmp_path: Path, bad: object) -> None:
        case = _yaml_case()
        case["turns"] = bad
        path = _write_dataset(tmp_path, "bad_turns.yaml", [case])
        with pytest.raises(EvalSetError, match="turns"):
            load_eval_set(path)


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

    def test_turns_change_fingerprint(self) -> None:
        """同一条末轮消息，单轮与多轮不是同一份评测：指纹必须能区分。"""
        last = "算了，先不订了"
        single = AgentEvalItem(id="a", name="a", message=last)
        multi = AgentEvalItem(id="a", name="a", message=last, turns=("我要订一间大床房", last))
        assert dataset_fingerprint([single]) != dataset_fingerprint([multi])


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


class TestDatasetToolAlignment:
    @pytest.mark.eval
    def test_eval_dataset_tools_registered(self) -> None:
        """防漂移：出厂数据集期望的工具名必须已在注册表注册。

        数据集引用了不存在（或已改名）的工具时，评测比对的是永远匹配不上的名字，
        指标会静默失真；此守卫在数据集与工具注册表之间拉一条硬连线。
        """
        from app.agents.toolkits.registry import default_registry

        registered = set(default_registry().names())
        missing: list[str] = []
        for fpath in sorted(DEFAULT_DATASET_DIR.glob("*.yaml")):
            for item in load_eval_set(fpath):
                if item.expected is None or not item.expected.tool_sequence:
                    continue
                for spec in item.expected.tool_sequence:
                    if spec.name not in registered:
                        missing.append(f"{fpath.name}:{item.id}:{spec.name}")
        assert not missing, f"数据集引用了未注册的工具：{missing}"


def _audit_entry(
    *,
    kind: Literal["model", "tool"] = "tool",
    seq: int = 1,
    status: str = "completed",
    tool_name: str | None = None,
    args_digest: dict[str, object] | None = None,
) -> AuditEntry:
    return AuditEntry(
        kind=kind,
        id=seq,
        run_id="run-1",
        seq=seq,
        agent_slug="front-desk",
        status=status,
        created_at=datetime(2026, 10, 16, tzinfo=UTC),
        tool_name=tool_name,
        args_digest=args_digest,
    )


class TestRealExecutorAuditReadback:
    """真实执行器的审计回读口径（纯函数，不触库不触图）。"""

    pytestmark = pytest.mark.eval

    def test_tool_calls_ordered_by_seq_and_filtered(self) -> None:
        entries = [
            _audit_entry(seq=3, tool_name="create_order", args_digest={"room_type_code": "A"}),
            _audit_entry(kind="model", seq=2),
            _audit_entry(seq=1, tool_name="list_room_types"),
        ]
        calls = tool_calls_from_audit(entries)
        assert [call["name"] for call in calls] == ["list_room_types", "create_order"]
        assert calls[0]["args"] == {}
        assert calls[1]["args"] == {"room_type_code": "A"}

    def test_task_success_rules(self) -> None:
        failed = [_audit_entry(seq=1, status="failed", tool_name="create_order")]
        ok = [_audit_entry(seq=1, status="completed", tool_name="list_room_types")]
        assert task_success_from("completed", ok) is True
        assert task_success_from("completed", []) is True
        assert task_success_from("completed", failed) is False
        assert task_success_from("failed", ok) is False
        assert task_success_from("cancelled", ok) is False

    def test_executor_identity_distinguishes_scripted_and_real(self) -> None:
        assert executor_identity(ScriptedCaseExecutor()) == "scripted"
        assert executor_identity(RealCaseExecutor()) == "real-graph"

    def test_config_fingerprint_covers_executor_and_judge(self) -> None:
        settings = get_settings()
        scripted = config_fingerprint_value(settings, "scripted", "judge-a")
        real = config_fingerprint_value(settings, "real-graph", "judge-a")
        other_judge = config_fingerprint_value(settings, "real-graph", "judge-b")
        assert scripted != real
        assert real != other_judge

    @pytest.mark.anyio
    async def test_execute_without_model_key_fails_loudly(self) -> None:
        settings = get_settings().model_copy(update={"model_api_key": ""})
        executor = RealCaseExecutor(settings=settings)
        with pytest.raises(EvalRunError, match="模型密钥"):
            await executor.execute(_make_item("no-key"))


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
        """出厂数据集存在且可加载；规模、多轮与覆盖类别是 P2.2 的验收面，做成守卫。"""
        assert DEFAULT_DATASET_DIR.is_dir()
        yaml_files = list(DEFAULT_DATASET_DIR.glob("*.yaml"))
        assert len(yaml_files) >= 3  # orders, checkin, dispatch
        items = [item for fpath in yaml_files for item in load_eval_set(fpath)]
        assert len(items) >= 50, "Agent 评测集规模下限（P2.2：8 → 50+）"
        assert any(item.turns for item in items), "缺少多轮用例"
        covered = {tag for item in items for tag in item.tags}
        required = {
            "order",
            "reschedule",
            "cancel",
            "invoice",
            "dispatch",
            "handoff",
            "multi-turn",
            "edge",
        }
        assert required <= covered, f"覆盖类别缺失：{sorted(required - covered)}"
