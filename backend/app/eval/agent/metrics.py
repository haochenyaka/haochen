"""智能体评估指标：工具链比对与任务成功率，宏观平均。

口径：
- tool_accuracy = 命中的预期工具调用数 ÷ 预期工具调用数（空预期记 1.0）；
- args_match 检查预期 args_contain 是否实际参数的超集；
- task_success 直接取工具链是否产生预期业务结果。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.eval.agent.dataset import AgentEvalItem, ToolCallSpec
from app.eval.agent.judge import JudgeResult


@dataclass(frozen=True)
class AgentQuestionOutcome:
    """逐题结果：工具链比对明细与可选的答案评判。"""

    item_id: str
    name: str
    question: str
    expected_sequence: tuple[dict[str, object], ...]
    actual_sequence: tuple[dict[str, object], ...]
    tool_accuracy: float
    args_match_rate: float
    task_success: bool | None
    expected_success: bool | None
    tags: tuple[str, ...]
    answer: str | None = None
    judge: dict[str, object] | None = None
    judge_score: float | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "item_id": self.item_id,
            "name": self.name,
            "question": self.question,
            "expected_sequence": list(self.expected_sequence),
            "actual_sequence": list(self.actual_sequence),
            "tool_accuracy": round(self.tool_accuracy, 6),
            "args_match_rate": round(self.args_match_rate, 6),
            "task_success": self.task_success,
            "expected_success": self.expected_success,
            "tags": list(self.tags),
        }
        if self.answer is not None:
            payload["answer"] = self.answer
        if self.judge is not None:
            payload["judge"] = dict(self.judge)
        if self.judge_score is not None:
            payload["judge_score"] = round(self.judge_score, 6)
        return payload


@dataclass(frozen=True)
class AgentEvalMetrics:
    """聚合指标：宏观平均。"""

    questions: int
    avg_tool_accuracy: float
    avg_args_match_rate: float
    task_success_rate: float | None
    total_passed: int
    by_tag: dict[str, dict[str, float]]
    avg_judge_score: float | None = None
    judge_scored: int = 0
    judge_pass_rate: float | None = None
    judge_dimensions: dict[str, float] | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "questions": self.questions,
            "avg_tool_accuracy": round(self.avg_tool_accuracy, 6),
            "avg_args_match_rate": round(self.avg_args_match_rate, 6),
            "total_passed": self.total_passed,
            "by_tag": self.by_tag,
        }
        if self.task_success_rate is not None:
            payload["task_success_rate"] = round(self.task_success_rate, 6)
        if self.avg_judge_score is not None:
            payload["avg_judge_score"] = round(self.avg_judge_score, 6)
            payload["judge_scored"] = self.judge_scored
        if self.judge_pass_rate is not None:
            payload["judge_pass_rate"] = round(self.judge_pass_rate, 6)
        if self.judge_dimensions is not None:
            payload["judge_dimensions"] = {
                name: round(value, 6) for name, value in self.judge_dimensions.items()
            }
        return payload


def score_question(
    item: AgentEvalItem,
    actual_tool_calls: list[dict[str, object]],
    *,
    task_success: bool | None = None,
    answer: str | None = None,
    judge_result: JudgeResult | None = None,
) -> AgentQuestionOutcome:
    """按题打分：比对预期与实际工具序列；用例未定义 expected 时按空预期处理。"""
    expected = item.expected
    expected_specs: list[ToolCallSpec] = []
    if expected is not None and expected.tool_sequence is not None:
        expected_specs = expected.tool_sequence
    tool_accuracy = _compare_sequence(expected_specs, actual_tool_calls)
    args_match_rate = _compare_args(expected_specs, actual_tool_calls)
    judge = judge_result.as_dict() if judge_result is not None else None
    return AgentQuestionOutcome(
        item_id=item.id,
        name=item.name,
        question=item.message,
        expected_sequence=tuple(spec.as_dict() for spec in expected_specs),
        actual_sequence=tuple(actual_tool_calls),
        tool_accuracy=tool_accuracy,
        args_match_rate=args_match_rate,
        task_success=task_success,
        expected_success=expected.task_success if expected is not None else None,
        tags=item.tags,
        answer=answer,
        judge=judge,
        judge_score=judge_result.overall if judge_result is not None else None,
    )


def aggregate(outcomes: list[AgentQuestionOutcome]) -> AgentEvalMetrics:
    """宏观平均：每题等权，按标签分组。"""
    total = len(outcomes)
    if total == 0:
        raise ValueError("评估结果为空，无法聚合")
    passed = sum(
        1
        for o in outcomes
        if o.tool_accuracy >= 1.0
        and (o.expected_success is None or o.task_success == o.expected_success)
    )
    success_rates = [
        o.task_success
        for o in outcomes
        if o.task_success is not None and o.expected_success is not None
    ]
    task_success_rate = (
        sum(1 for s in success_rates if s) / len(success_rates) if success_rates else None
    )
    # by-tag aggregation
    tag_groups: dict[str, list[AgentQuestionOutcome]] = {}
    for outcome in outcomes:
        for tag in outcome.tags:
            tag_groups.setdefault(tag, []).append(outcome)
    by_tag: dict[str, dict[str, float]] = {}
    for tag, group in sorted(tag_groups.items()):
        g_total = len(group)
        g_passed = sum(
            1
            for o in group
            if o.tool_accuracy >= 1.0
            and (o.expected_success is None or o.task_success == o.expected_success)
        )
        by_tag[tag] = {
            "count": g_total,
            "pass_rate": round(g_passed / g_total, 6) if g_total else 0.0,
        }
    judged = [o for o in outcomes if o.judge_score is not None]
    judge_passed = sum(1 for o in judged if _judge_passed(o))
    return AgentEvalMetrics(
        questions=total,
        avg_tool_accuracy=_mean([o.tool_accuracy for o in outcomes]),
        avg_args_match_rate=_mean([o.args_match_rate for o in outcomes]),
        task_success_rate=task_success_rate,
        total_passed=passed,
        by_tag=by_tag,
        avg_judge_score=_mean([o.judge_score for o in judged if o.judge_score is not None])
        if judged
        else None,
        judge_scored=len(judged),
        judge_pass_rate=judge_passed / len(judged) if judged else None,
        judge_dimensions=_dimension_means(judged),
    )


def _judge_passed(outcome: AgentQuestionOutcome) -> bool:
    judge = outcome.judge or {}
    return bool(judge.get("passed", False))


def _dimension_means(outcomes: list[AgentQuestionOutcome]) -> dict[str, float] | None:
    """四维分均值：只统计有评判结果的题目。"""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for outcome in outcomes:
        dimensions = (outcome.judge or {}).get("dimensions")
        if not isinstance(dimensions, dict):
            continue
        for name, payload in dimensions.items():
            if not isinstance(payload, dict):
                continue
            value = payload.get("score")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            sums[name] = sums.get(name, 0.0) + float(value)
            counts[name] = counts.get(name, 0) + 1
    if not sums:
        return None
    return {name: sums[name] / counts[name] for name in sorted(sums)}


def _compare_sequence(expected: list[ToolCallSpec], actual: list[dict[str, object]]) -> float:
    """工具序列准确率：顺序匹配的预期调用数 ÷ 预期总数。"""
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    hits = 0
    e_idx = 0
    for call in actual:
        call_name = call.get("name", "")
        if e_idx < len(expected) and call_name == expected[e_idx].name:
            hits += 1
            e_idx += 1
    return hits / len(expected)


def _compare_args(expected: list[ToolCallSpec], actual: list[dict[str, object]]) -> float:
    """参数匹配率：args_contain 约束全部满足的预期调用比例。

    实际调用格式为 {"name": "...", "args": {...}}，args_contain 从 args 子字典匹配。
    """
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    matched = 0
    for spec in expected:
        spec_args = spec.args_contain or {}
        if not spec_args:
            matched += 1
            continue
        # 找到同名工具的第一次调用
        for call in actual:
            if call.get("name") != spec.name:
                continue
            call_args = call.get("args", {})
            if isinstance(call_args, dict) and all(
                call_args.get(k) == v for k, v in spec_args.items()
            ):
                matched += 1
                break
    return matched / len(expected) if expected else 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0
