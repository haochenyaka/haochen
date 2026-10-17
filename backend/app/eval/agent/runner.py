"""智能体评估运行器：加载 YAML 数据集 → 逐条执行 → 工具链比对 → 聚合 → 报告落库。

不变量：
- 执行器可注入（测试用确定性替身，生产用真实 Agent Graph）；
- 报告不可变：新报告落库时，同数据集同模型同配置的旧报告被标记作废；
- 可比性三要素：dataset_fingerprint、config_fingerprint、model。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.eval.agent.dataset import (
    AgentEvalItem,
    dataset_fingerprint,
    fingerprint,
    load_eval_set,
)
from app.eval.agent.judge import AgentAnswerJudge, build_agent_judge
from app.eval.agent.metrics import (
    AgentQuestionOutcome,
    aggregate,
    score_question,
)
from app.repositories.eval_agent_repo import AgentEvalReportRepository

logger = logging.getLogger(__name__)

DEFAULT_DATASET_DIR = Path(__file__).parent / "datasets"


class EvalRunError(RuntimeError):
    """评估无法执行：数据集缺失、配置非法。"""


@dataclass(frozen=True)
class CaseExecution:
    """单条用例的执行事实：工具序列、任务成功标志与末轮回答文本。"""

    tool_calls: tuple[dict[str, object], ...]
    task_success: bool | None
    answer: str | None = None


class CaseExecutor(Protocol):
    """单条用例执行器：输入用例，返回工具序列、任务成功标志与回答文本。"""

    async def execute(self, item: AgentEvalItem) -> CaseExecution: ...


@dataclass(frozen=True)
class AgentEvalReportData:
    """一次评估的完整事实。"""

    dataset_path: str
    dataset_fingerprint: str
    config_fingerprint: str
    model: str | None
    total_cases: int
    metrics: dict[str, Any]
    outcomes: list[dict[str, Any]]
    generated_at: str
    report_id: int | None = None
    superseded_reports: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "dataset_path": self.dataset_path,
            "dataset_fingerprint": self.dataset_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "model": self.model,
            "total_cases": self.total_cases,
            "metrics": dict(self.metrics),
            "superseded_reports": list(self.superseded_reports),
            "outcomes": [dict(item) for item in self.outcomes],
        }


def config_fingerprint_value(
    settings: Settings,
    executor_name: str | None = None,
    judge_name: str | None = None,
) -> str:
    """配置指纹：模型名、执行器身份与评判器身份（模型/温度/采样数）。

    执行器身份必须入指纹——脚本替身与真实图在同一数据集同一模型下产出的分数不可比，
    不区分会让两类报告互相作废。
    """
    payload: dict[str, object] = {
        "model": settings.model_name or "none",
        "executor": executor_name,
        "judge": judge_name,
    }
    return fingerprint(payload)


def executor_identity(executor: CaseExecutor) -> str:
    """执行器身份：显式声明优先，缺省退化为类名。"""
    return str(getattr(executor, "name", None) or type(executor).__name__)


async def evaluate_items(
    *,
    items: Sequence[AgentEvalItem],
    executor: CaseExecutor,
    settings: Settings | None = None,
    judge: AgentAnswerJudge | None = None,
    dataset_path: str | Path = "",
) -> AgentEvalReportData:
    """纯评估路径（不触库）：逐条执行比对，可选评判，最后宏观聚合。"""
    settings = settings or get_settings()
    outcomes: list[AgentQuestionOutcome] = []
    active_judge = judge if judge is not None and judge.available else None
    for item in items:
        execution = await executor.execute(item)
        judge_result = None
        if active_judge is not None and execution.answer is not None:
            judge_result = await active_judge.evaluate(
                item,
                answer=execution.answer,
                tool_calls=execution.tool_calls,
            )
        outcome = score_question(
            item,
            list(execution.tool_calls),
            task_success=execution.task_success,
            answer=execution.answer,
            judge_result=judge_result,
        )
        outcomes.append(outcome)
    metrics = aggregate(outcomes)
    cfp = config_fingerprint_value(
        settings,
        executor_identity(executor),
        active_judge.identity if active_judge is not None else None,
    )
    return AgentEvalReportData(
        dataset_path=str(dataset_path),
        dataset_fingerprint=dataset_fingerprint(items),
        config_fingerprint=cfp,
        model=settings.model_name,
        total_cases=len(items),
        metrics=metrics.as_dict(),
        outcomes=[outcome.as_dict() for outcome in outcomes],
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


async def run_eval(
    session: AsyncSession,
    *,
    dataset_path: Path,
    executor: CaseExecutor | None = None,
    settings: Settings | None = None,
) -> AgentEvalReportData:
    """完整评估路径：加载数据集 → 执行 → 聚合。

    执行器缺省时用 ScriptedCaseExecutor（仅用于测试）。
    """
    settings = settings or get_settings()
    items = load_eval_set(dataset_path)
    active_executor = executor or ScriptedCaseExecutor()
    judge = build_agent_judge(settings)
    return await evaluate_items(
        items=items,
        executor=active_executor,
        settings=settings,
        judge=judge,
        dataset_path=str(dataset_path),
    )


async def persist_report(session: AsyncSession, report: AgentEvalReportData) -> AgentEvalReportData:
    """报告落库：同数据集 + 同模型 + 同配置下旧报告作废。"""
    repository = AgentEvalReportRepository(session)
    previous = await repository.list_by_dataset(report.dataset_fingerprint)
    row = await repository.create(
        dataset_path=report.dataset_path,
        dataset_fingerprint=report.dataset_fingerprint,
        config_fingerprint=report.config_fingerprint,
        model=report.model,
        total_cases=report.total_cases,
        metrics=report.metrics,
        outcomes=report.outcomes,
    )
    superseded: list[int] = []
    for old in previous:
        if old.superseded_by is not None:
            continue
        if old.config_fingerprint == report.config_fingerprint and old.model == report.model:
            continue
        reason = _invalidation_reason(old, report)
        if await repository.mark_superseded(old.id, superseded_by=row.id, reason=reason):
            superseded.append(old.id)
    await session.commit()
    return AgentEvalReportData(
        dataset_path=report.dataset_path,
        dataset_fingerprint=report.dataset_fingerprint,
        config_fingerprint=report.config_fingerprint,
        model=report.model,
        total_cases=report.total_cases,
        metrics=report.metrics,
        outcomes=report.outcomes,
        generated_at=report.generated_at,
        report_id=row.id,
        superseded_reports=tuple(superseded),
    )


class ScriptedCaseExecutor:
    """确定性执行器（测试用）：返回预设的工具调用列表。"""

    name = "scripted"

    def __init__(
        self,
        tool_calls: list[dict[str, object]] | None = None,
        task_success: bool | None = True,
        answer: str | None = None,
    ) -> None:
        self._tool_calls = tool_calls or []
        self._task_success = task_success
        self._answer = answer

    async def execute(self, item: AgentEvalItem) -> CaseExecution:
        return CaseExecution(
            tool_calls=tuple(self._tool_calls),
            task_success=self._task_success,
            answer=self._answer,
        )


def _invalidation_reason(old: object, report: AgentEvalReportData) -> str:
    """判定作废原因：通过 getattr 兼容 ORM 对象与 dataclass。"""
    old_cfg = getattr(old, "config_fingerprint", "")
    old_model = getattr(old, "model", "")
    cfg_changed = old_cfg != report.config_fingerprint
    model_changed = old_model != report.model
    if cfg_changed and model_changed:
        return "config_and_model_changed"
    return "config_changed" if cfg_changed else "model_changed"
