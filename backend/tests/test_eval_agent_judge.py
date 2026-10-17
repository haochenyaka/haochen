"""Agent 答案评判与校准：解析、自一致性、指标接入与 Cohen's kappa。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.config import get_settings
from app.eval.agent.calibration import (
    DEFAULT_CALIBRATION_PATH,
    CalibrationCase,
    agreement,
    load_calibration_set,
    run_calibration,
)
from app.eval.agent.dataset import AgentEvalItem, ExpectedSpec, ToolCallSpec
from app.eval.agent.judge import (
    JUDGE_DIMENSIONS,
    AgentAnswerJudge,
    DimensionScore,
    JudgeResult,
    parse_judgement,
)
from app.eval.agent.metrics import aggregate, score_question
from app.eval.agent.runner import ScriptedCaseExecutor, evaluate_items


@pytest.fixture
def anyio_backend() -> str:
    """judge 内部用 asyncio.gather 做自一致性采样，只跑 asyncio 后端。"""
    return "asyncio"


def _score_payload(
    relevance: float = 1.0,
    factuality: float = 1.0,
    tone: float = 1.0,
    boundary: float = 1.0,
) -> str:
    return json.dumps(
        {
            "relevance": {"score": relevance, "reason": "回应请求"},
            "factuality": {"score": factuality, "reason": "与事实一致"},
            "tone": {"score": tone, "reason": "语气得体"},
            "boundary": {"score": boundary, "reason": "未越界"},
        },
        ensure_ascii=False,
    )


class FakeChatModel:
    """按顺序返回预设内容的假模型；记录调用次数。"""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.calls = 0

    async def ainvoke(self, messages: object) -> AIMessage:
        reply = self._replies[self.calls % len(self._replies)]
        self.calls += 1
        return AIMessage(content=reply)


class StubJudge:
    """校准测试替身：按 item.id 返回预设评判结果。"""

    threshold = 0.6
    model_name = "stub-judge"
    identity = "stub-judge:t=0.7:n=1"

    def __init__(self, results: dict[str, JudgeResult | None]) -> None:
        self._results = results

    async def evaluate(
        self, item: AgentEvalItem, *, answer: str, tool_calls: Sequence[dict] = ()
    ) -> JudgeResult | None:
        return self._results.get(item.id)


def _judge_result(
    relevance: float = 1.0,
    factuality: float = 1.0,
    tone: float = 1.0,
    boundary: float = 1.0,
) -> JudgeResult:
    scores = {
        "relevance": relevance,
        "factuality": factuality,
        "tone": tone,
        "boundary": boundary,
    }
    return JudgeResult(
        dimensions={
            name: DimensionScore(score=scores[name], reason="stub") for name in JUDGE_DIMENSIONS
        },
        samples=3,
        model="stub-judge",
        threshold=0.6,
    )


def _item(item_id: str = "test-001") -> AgentEvalItem:
    return AgentEvalItem(
        id=item_id,
        name=item_id,
        message="早餐几点开始？",
        expected=ExpectedSpec(tool_sequence=[ToolCallSpec(name="query_kb")], task_success=True),
        tags=("consult",),
    )


class TestParseJudgement:
    def test_valid_json(self) -> None:
        parsed = parse_judgement(_score_payload(relevance=0.8))
        assert parsed is not None
        assert parsed["relevance"].score == 0.8
        assert parsed["relevance"].reason == "回应请求"

    def test_code_fence_and_surrounding_text(self) -> None:
        text = "以下是评分：\n```json\n" + _score_payload() + "\n```\n完毕"
        parsed = parse_judgement(text)
        assert parsed is not None
        assert parsed["factuality"].score == 1.0

    def test_ten_point_scale_normalized(self) -> None:
        parsed = parse_judgement(_score_payload(relevance=8))
        assert parsed is not None
        assert parsed["relevance"].score == 0.8

    def test_missing_dimension_invalid(self) -> None:
        payload = json.loads(_score_payload())
        payload.pop("boundary")
        assert parse_judgement(json.dumps(payload)) is None

    def test_out_of_range_invalid(self) -> None:
        assert parse_judgement(_score_payload(relevance=42)) is None

    def test_plain_text_invalid(self) -> None:
        assert parse_judgement("回答不错，给 0.9 分。") is None


class TestAgentAnswerJudge:
    @pytest.mark.anyio
    async def test_self_consistency_uses_median(self) -> None:
        settings = get_settings().model_copy(update={"agent_judge_samples": 3})
        model = FakeChatModel(
            [
                _score_payload(relevance=0.2),
                _score_payload(relevance=0.8),
                _score_payload(relevance=0.6),
            ]
        )
        judge = AgentAnswerJudge(settings, model=model)
        result = await judge.evaluate(_item(), answer="早餐 7 点开始。")
        assert result is not None
        assert model.calls == 3
        assert result.samples == 3
        assert result.dimensions["relevance"].score == 0.6
        assert result.dimensions["factuality"].score == 1.0

    @pytest.mark.anyio
    async def test_all_unparseable_returns_none(self) -> None:
        settings = get_settings().model_copy(update={"agent_judge_samples": 2})
        judge = AgentAnswerJudge(settings, model=FakeChatModel(["不知道", "再想想"]))
        assert await judge.evaluate(_item(), answer="...") is None

    @pytest.mark.anyio
    async def test_one_dimension_failure_fails_overall(self) -> None:
        settings = get_settings().model_copy(update={"agent_judge_samples": 1})
        judge = AgentAnswerJudge(settings, model=FakeChatModel([_score_payload(factuality=0.1)]))
        result = await judge.evaluate(_item(), answer="编造的事实")
        assert result is not None
        assert result.passed is False

    def test_identity_covers_temperature_and_samples(self) -> None:
        settings = get_settings().model_copy(
            update={"agent_judge_temperature": 0.9, "agent_judge_samples": 5}
        )
        judge = AgentAnswerJudge(settings, model=FakeChatModel([_score_payload()]))
        assert "t=0.9" in judge.identity
        assert "n=5" in judge.identity


class TestJudgeIntegration:
    @pytest.mark.anyio
    async def test_evaluate_items_records_judge_metrics(self) -> None:
        item = _item()
        executor = ScriptedCaseExecutor(
            tool_calls=[{"name": "query_kb", "args": {}}],
            task_success=True,
            answer="早餐 7:00 开始。",
        )
        settings = get_settings().model_copy(update={"agent_judge_samples": 1})
        judge = AgentAnswerJudge(
            settings, model=FakeChatModel([_score_payload(tone=0.9, boundary=0.8)])
        )
        report = await evaluate_items(
            items=[item],
            executor=executor,
            settings=settings,
            judge=judge,
        )
        assert report.outcomes[0]["answer"] == "早餐 7:00 开始。"
        assert report.outcomes[0]["judge"]["passed"] is True
        assert report.metrics["judge_scored"] == 1
        assert report.metrics["avg_judge_score"] == pytest.approx(0.925)
        assert report.metrics["judge_pass_rate"] == 1.0
        assert report.metrics["judge_dimensions"]["tone"] == 0.9

    def test_judge_score_participates_in_aggregation(self) -> None:
        item = _item()
        outcome = score_question(
            item,
            [{"name": "query_kb", "args": {}}],
            task_success=True,
            answer="早餐 7:00 开始。",
            judge_result=_judge_result(factuality=0.1),
        )
        metrics = aggregate([outcome])
        assert metrics.judge_scored == 1
        assert metrics.judge_pass_rate == 0.0
        assert metrics.avg_judge_score == pytest.approx(0.775)


class TestCalibration:
    def test_default_calibration_set_loads(self) -> None:
        cases = load_calibration_set(DEFAULT_CALIBRATION_PATH)
        assert 10 <= len(cases) <= 20
        assert all(set(case.human or {}) == set(JUDGE_DIMENSIONS) for case in cases)

    def test_kappa_perfect_agreement(self) -> None:
        stats = agreement([(True, True), (True, True), (False, False), (False, False)])
        assert stats.kappa == 1.0
        assert stats.agreement == 1.0

    def test_kappa_chance_level(self) -> None:
        stats = agreement([(True, True), (True, False), (True, True), (True, False)])
        assert stats.kappa == 0.0
        assert stats.true_positive == 2
        assert stats.false_positive == 2

    def test_kappa_perfect_disagreement(self) -> None:
        stats = agreement([(True, False), (False, True)])
        assert stats.kappa == -1.0

    def test_kappa_empty(self) -> None:
        stats = agreement([])
        assert stats.kappa is None
        assert stats.n == 0

    @pytest.mark.anyio
    async def test_run_calibration_reports_kappa(self) -> None:
        cases = [
            CalibrationCase(
                id="a",
                name="a",
                message="问题",
                answer="回答",
                human={name: 1.0 for name in JUDGE_DIMENSIONS},
            ),
            CalibrationCase(
                id="b",
                name="b",
                message="问题",
                answer="回答",
                human={name: 0.1 for name in JUDGE_DIMENSIONS},
            ),
        ]
        judge = StubJudge({"a": _judge_result(), "b": _judge_result(factuality=0.1)})
        report = await run_calibration(judge, cases)
        assert report.cases == 2
        assert report.valid == 2
        assert report.overall.kappa == 1.0
        assert report.by_dimension["factuality"].kappa == 1.0
        payload = report.as_dict()
        assert payload["overall"]["kappa"] == 1.0

    def test_calibration_set_rejects_out_of_range(self, tmp_path: Path) -> None:
        bad = {
            "id": "x",
            "message": "问题",
            "answer": "回答",
            "human": {"relevance": 2.0, "factuality": 1.0, "tone": 1.0, "boundary": 1.0},
        }
        path = tmp_path / "bad.jsonl"
        path.write_text(json.dumps(bad, ensure_ascii=False) + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="越界"):
            load_calibration_set(path)
