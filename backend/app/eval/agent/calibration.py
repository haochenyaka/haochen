"""Agent 答案评判校准：人工标注集 → 评判器 → Cohen's kappa。

口径：
- 人工标注按四维 0–1 分给出；某题「通过」= 四维都达到阈值（与评判器一致，避免均值稀释）；
- kappa 按二分类（通过 / 未通过）计算，同时报告四维各自的一致率与混淆计数；
- 校准集是评判器的验收面，不参与日常评测分母；模型或提示词变化后须重跑并在报告里留痕。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.eval.agent.dataset import AgentEvalItem, HistoryMessage
from app.eval.agent.judge import JUDGE_DIMENSIONS, AgentAnswerJudge, build_agent_judge

logger = logging.getLogger(__name__)

DEFAULT_CALIBRATION_PATH = Path(__file__).parent / "judge_calibration.jsonl"
DEFAULT_OUTPUT_PATH = (
    Path(__file__).resolve().parents[4] / "docs" / "eval" / "agent-judge-calibration.json"
)
MIN_CASES = 10
MAX_CASES = 20


class CalibrationSetError(ValueError):
    """校准集格式不合法：字段缺失、分数越界或 id 重复。"""


@dataclass(frozen=True)
class CalibrationCase:
    """一条人工标注样本：输入、证据、候选回答与四维人工分。"""

    id: str
    name: str
    message: str
    answer: str
    history: tuple[HistoryMessage, ...] = ()
    tool_calls: tuple[dict[str, object], ...] = ()
    human: dict[str, float] | None = None

    def as_agent_item(self) -> AgentEvalItem:
        return AgentEvalItem(
            id=self.id,
            name=self.name,
            message=self.message,
            history=self.history,
        )


@dataclass(frozen=True)
class AgreementStats:
    """二分类一致性与 Cohen's kappa；kappa 为 None 表示无有效样本。"""

    n: int
    agreement: float | None
    kappa: float | None
    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    def as_dict(self) -> dict[str, object]:
        return {
            "n": self.n,
            "agreement": _round_or_none(self.agreement),
            "kappa": _round_or_none(self.kappa),
            "true_positive": self.true_positive,
            "false_positive": self.false_positive,
            "true_negative": self.true_negative,
            "false_negative": self.false_negative,
        }


@dataclass(frozen=True)
class CalibrationReport:
    """校准结果：整体与逐维一致性 + 逐题明细。"""

    generated_at: str
    judge_model: str
    judge_identity: str
    threshold: float
    cases: int
    valid: int
    overall: AgreementStats
    by_dimension: dict[str, AgreementStats]
    outcomes: list[dict[str, Any]]

    def as_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "judge_model": self.judge_model,
            "judge_identity": self.judge_identity,
            "threshold": self.threshold,
            "cases": self.cases,
            "valid": self.valid,
            "overall": self.overall.as_dict(),
            "by_dimension": {name: stats.as_dict() for name, stats in self.by_dimension.items()},
            "outcomes": [dict(item) for item in self.outcomes],
        }

    def render(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n"


def load_calibration_set(path: Path) -> list[CalibrationCase]:
    """读 JSONL 校准集：逐行校验、id 唯一、样本数在 10–20 之间。"""
    cases: list[CalibrationCase] = []
    seen: set[str] = set()
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationSetError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
        case = _parse_case(path, line_no, payload)
        if case.id in seen:
            raise CalibrationSetError(f"{path}:{line_no} id 重复：{case.id}")
        seen.add(case.id)
        cases.append(case)
    if not MIN_CASES <= len(cases) <= MAX_CASES:
        raise CalibrationSetError(
            f"{path} 样本数须在 {MIN_CASES}–{MAX_CASES} 之间，实际 {len(cases)}"
        )
    return cases


async def run_calibration(
    judge: AgentAnswerJudge,
    cases: Sequence[CalibrationCase],
    *,
    threshold: float | None = None,
) -> CalibrationReport:
    """逐条评判并与人工标注比对；评判结果缺失的题不进入任何分母。"""
    active_threshold = judge.threshold if threshold is None else threshold
    outcomes: list[dict[str, Any]] = []
    overall_pairs: list[tuple[bool, bool]] = []
    dimension_pairs: dict[str, list[tuple[bool, bool]]] = {name: [] for name in JUDGE_DIMENSIONS}
    for case in cases:
        human = case.human or {}
        result = await judge.evaluate(
            case.as_agent_item(),
            answer=case.answer,
            tool_calls=case.tool_calls,
        )
        outcome: dict[str, Any] = {
            "id": case.id,
            "name": case.name,
            "human": dict(human),
            "judge": result.as_dict() if result is not None else None,
        }
        if result is not None:
            human_passed = _passed(human, active_threshold)
            judge_passed = (
                result.passed
                if threshold is None
                else _passed(
                    {name: item.score for name, item in result.dimensions.items()},
                    active_threshold,
                )
            )
            overall_pairs.append((judge_passed, human_passed))
            for name in JUDGE_DIMENSIONS:
                dimension_pairs[name].append(
                    (
                        result.dimensions[name].score >= active_threshold,
                        human[name] >= active_threshold,
                    )
                )
            outcome["human_passed"] = human_passed
            outcome["judge_passed"] = judge_passed
        outcomes.append(outcome)
    return CalibrationReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        judge_model=result_model(judge),
        judge_identity=judge.identity,
        threshold=active_threshold,
        cases=len(cases),
        valid=len(overall_pairs),
        overall=agreement(overall_pairs),
        by_dimension={name: agreement(pairs) for name, pairs in dimension_pairs.items()},
        outcomes=outcomes,
    )


def agreement(pairs: Sequence[tuple[bool, bool]]) -> AgreementStats:
    """二分类一致性 + Cohen's kappa（judge 为预测，human 为真值）。"""
    n = len(pairs)
    if n == 0:
        return AgreementStats(0, None, None, 0, 0, 0, 0)
    tp = sum(1 for judge, human in pairs if judge and human)
    fp = sum(1 for judge, human in pairs if judge and not human)
    tn = sum(1 for judge, human in pairs if not judge and not human)
    fn = sum(1 for judge, human in pairs if not judge and human)
    observed = (tp + tn) / n
    judge_positive = (tp + fp) / n
    human_positive = (tp + fn) / n
    expected = judge_positive * human_positive + (1 - judge_positive) * (1 - human_positive)
    if expected >= 1.0:
        kappa = 1.0 if observed >= 1.0 else 0.0
    else:
        kappa = (observed - expected) / (1 - expected)
    return AgreementStats(
        n=n,
        agreement=observed,
        kappa=kappa,
        true_positive=tp,
        false_positive=fp,
        true_negative=tn,
        false_negative=fn,
    )


def result_model(judge: AgentAnswerJudge) -> str:
    return judge.model_name or "unknown"


def _parse_case(path: Path, line_no: int, payload: object) -> CalibrationCase:
    if not isinstance(payload, dict):
        raise CalibrationSetError(f"{path}:{line_no} 必须是对象")
    for key in ("id", "message", "answer", "human"):
        if key not in payload:
            raise CalibrationSetError(f"{path}:{line_no} 缺少字段：{key}")
    case_id = str(payload["id"]).strip()
    message = str(payload["message"]).strip()
    answer = str(payload["answer"]).strip()
    if not case_id or not message or not answer:
        raise CalibrationSetError(f"{path}:{line_no} id/message/answer 不能为空")
    history = _parse_history(payload.get("history"))
    tool_calls = _parse_tool_calls(payload.get("tool_calls"))
    human = _parse_human(path, line_no, payload["human"])
    return CalibrationCase(
        id=case_id,
        name=str(payload.get("name") or case_id).strip(),
        message=message,
        answer=answer,
        history=history,
        tool_calls=tool_calls,
        human=human,
    )


def _parse_history(raw: object) -> tuple[HistoryMessage, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CalibrationSetError("history 必须为数组")
    parsed: list[HistoryMessage] = []
    for item in raw:
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            raise CalibrationSetError("history 条目缺少 role/content")
        parsed.append(HistoryMessage(role=str(item["role"]), content=str(item["content"])))
    return tuple(parsed)


def _parse_tool_calls(raw: object) -> tuple[dict[str, object], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise CalibrationSetError("tool_calls 必须为数组")
    calls: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or "name" not in item:
            raise CalibrationSetError("tool_calls 条目缺少 name")
        calls.append({"name": str(item["name"]), "args": dict(item.get("args") or {})})
    return tuple(calls)


def _parse_human(path: Path, line_no: int, raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise CalibrationSetError(f"{path}:{line_no} human 必须为对象")
    scores: dict[str, float] = {}
    for name in JUDGE_DIMENSIONS:
        value = raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CalibrationSetError(f"{path}:{line_no} human.{name} 必须为 0–1 数字")
        score = float(value)
        if not 0.0 <= score <= 1.0:
            raise CalibrationSetError(f"{path}:{line_no} human.{name} 越界：{score}")
        scores[name] = score
    return scores


def _passed(scores: dict[str, float], threshold: float) -> bool:
    return all(scores[name] >= threshold for name in JUDGE_DIMENSIONS)


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 6)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 答案评判校准：人工标注集 → Cohen's kappa")
    parser.add_argument("--set", dest="set_path", default=str(DEFAULT_CALIBRATION_PATH))
    parser.add_argument("--out", dest="out_path", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--model", default="", help="评判模型名（默认复用 MODEL_NAME）")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--samples", type=int, default=None, help="每題自一致性采样次数")
    parser.add_argument("--threshold", type=float, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    settings = get_settings()
    updates: dict[str, Any] = {}
    if args.model:
        updates["agent_judge_model_name"] = args.model
    if args.temperature is not None:
        updates["agent_judge_temperature"] = args.temperature
    if args.samples is not None:
        updates["agent_judge_samples"] = args.samples
    if args.threshold is not None:
        updates["agent_judge_pass_threshold"] = args.threshold
    judge = build_agent_judge(settings.model_copy(update=updates))
    if judge is None:
        logger.error("未配置模型密钥，无法运行评判校准")
        return 2
    cases = load_calibration_set(Path(args.set_path))
    report = asyncio.run(run_calibration(judge, cases))
    out_path = Path(args.out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report.render(), encoding="utf-8")
    stats = report.overall.as_dict()
    logger.info(
        "校准完成：cases=%d valid=%d agreement=%s kappa=%s → %s",
        report.cases,
        report.valid,
        stats["agreement"],
        stats["kappa"],
        out_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
