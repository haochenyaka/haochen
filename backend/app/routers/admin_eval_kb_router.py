"""知识库评估接口：列出可用数据集、触发评估、查询/作废报告。

权限：admin/manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.eval.kb_dataset import EvalSetError, load_eval_set
from app.eval.kb_runner import (
    DEFAULT_K,
    DEFAULT_SET_PATH,
    EvalReport,
    EvalRunError,
    PersistOutcome,
)
from app.eval.kb_runner import (
    persist_report as persist_kb_report,
)
from app.eval.kb_runner import run_eval as run_kb_eval
from app.knowledge.retrieval import RetrievalError
from app.repositories.eval_repository import KbEvalReportRepository
from app.security import Identity, require_roles

router = APIRouter(prefix="/admin/eval/kb", tags=["admin-eval-kb"])

eval_guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(eval_guard)]


class DatasetInfo(BaseModel):
    path: str
    name: str
    description: str
    case_count: int


class RunRequest(BaseModel):
    dataset_path: str = str(DEFAULT_SET_PATH)
    k: int = DEFAULT_K


class MetricsModel(BaseModel):
    questions: int
    k: int
    recall_at_k: float
    precision_at_k: float
    f1_at_k: float
    primary_top1_rate: float
    hit_rate: float
    answer_correctness: float | None
    overall_score: float
    overall_metric: str


class OutcomeModel(BaseModel):
    item_id: str
    question: str
    gold_titles: list[str]
    primary_title: str | None
    recall: float
    precision: float
    f1: float
    primary_hit: bool
    hit: bool
    answer_correctness: float | None


class ReportItem(BaseModel):
    id: int
    dataset_path: str
    dataset_fingerprint: str
    content_fingerprint: str
    config_fingerprint: str
    k: int
    questions: int
    evaluator: str | None
    metrics: MetricsModel
    superseded_by: int | None
    invalidated_reason: str | None
    created_at: str


class ReportDetail(ReportItem):
    outcomes: list[OutcomeModel]
    retrieval_config: dict[str, object]
    comparable_reports: list[int]
    superseded_reports: list[int]


class InvalidatedBy(BaseModel):
    reason: str


@router.get("/datasets", response_model=list[DatasetInfo])
async def list_datasets(_identity: GuardDep) -> list[DatasetInfo]:
    """列出可用评估数据集。"""
    from app.eval.kb_dataset import load_eval_set

    default_path = DEFAULT_SET_PATH
    if not default_path.is_file():
        return []
    try:
        items = load_eval_set(default_path)
        return [
            DatasetInfo(
                path=str(default_path),
                name=default_path.stem,
                description=f"出厂评估集（{len(items)} 题）",
                case_count=len(items),
            )
        ]
    except Exception:
        return []


@router.post("/run", response_model=ReportDetail)
async def run_kb_eval_endpoint(
    body: RunRequest,
    session: SessionDep,
    _identity: GuardDep,
) -> ReportDetail:
    """触发知识库评估。"""
    dataset_path = Path(body.dataset_path)
    if not dataset_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"评估集不存在：{dataset_path}",
        )
    try:
        items = load_eval_set(dataset_path)
    except EvalSetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    try:
        report = await run_kb_eval(session, items=items, k=body.k, dataset_path=dataset_path)
    except EvalRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    except RetrievalError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"检索服务不可用：{exc}",
        )
    outcome = await persist_kb_report(session, report)
    await session.commit()
    return _build_detail(report, outcome)


@router.get("/reports", response_model=list[ReportItem])
async def list_reports(
    session: SessionDep,
    _identity: GuardDep,
    limit: int = 20,
) -> list[ReportItem]:
    """列出最近评估报告。"""
    repo = KbEvalReportRepository(session)
    rows = await repo.list_recent(limit=limit)
    return [
        ReportItem(
            id=row.id,
            dataset_path=row.dataset_path,
            dataset_fingerprint=row.dataset_fingerprint,
            content_fingerprint=row.content_fingerprint,
            config_fingerprint=row.config_fingerprint,
            k=row.k,
            questions=row.questions,
            evaluator=row.evaluator,
            metrics=_to_metrics_model(row.metrics),
            superseded_by=row.superseded_by,
            invalidated_reason=row.invalidated_reason,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@router.get("/reports/{report_id}", response_model=ReportDetail)
async def get_report(
    report_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> ReportDetail:
    """报告详情（含逐条结果）。"""
    repo = KbEvalReportRepository(session)
    row = await repo.get(report_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    return _to_report_detail(row)


@router.post("/reports/{report_id}/invalidate", response_model=ReportItem)
async def invalidate_report(
    report_id: int,
    session: SessionDep,
    _identity: GuardDep,
    body: InvalidatedBy | None = None,
) -> ReportItem:
    """手动作废报告。"""
    repo = KbEvalReportRepository(session)
    row = await repo.get(report_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    reason = body.reason if body else "manual_invalidation"
    await repo.mark_superseded(report_id, superseded_by=report_id, reason=reason)
    await session.commit()
    row = await repo.get(report_id)
    assert row is not None
    return ReportItem(
        id=row.id,
        dataset_path=row.dataset_path,
        dataset_fingerprint=row.dataset_fingerprint,
        content_fingerprint=row.content_fingerprint,
        config_fingerprint=row.config_fingerprint,
        k=row.k,
        questions=row.questions,
        evaluator=row.evaluator,
        metrics=_to_metrics_model(row.metrics),
        superseded_by=row.superseded_by,
        invalidated_reason=row.invalidated_reason,
        created_at=row.created_at.isoformat(),
    )


def _to_metrics_model(metrics: dict[str, object]) -> MetricsModel:
    return MetricsModel(
        questions=int(metrics.get("questions", 0)),
        k=int(metrics.get("k", DEFAULT_K)),
        recall_at_k=float(metrics.get("recall_at_k", 0.0)),
        precision_at_k=float(metrics.get("precision_at_k", 0.0)),
        f1_at_k=float(metrics.get("f1_at_k", 0.0)),
        primary_top1_rate=float(metrics.get("primary_top1_rate", 0.0)),
        hit_rate=float(metrics.get("hit_rate", 0.0)),
        answer_correctness=metrics.get("answer_correctness"),
        overall_score=float(metrics.get("overall_score", 0.0)),
        overall_metric=str(metrics.get("overall_metric", "recall_at_k")),
    )


def _build_detail(report: EvalReport, outcome: PersistOutcome) -> ReportDetail:
    return ReportDetail(
        id=outcome.report_id,
        dataset_path=report.dataset_path,
        dataset_fingerprint=report.dataset_fingerprint,
        content_fingerprint=report.content_fingerprint,
        config_fingerprint=report.config_fingerprint,
        k=report.k,
        questions=len(report.outcomes),
        evaluator=report.evaluator,
        metrics=_to_metrics_model(report.metrics),
        outcomes=[
            OutcomeModel(
                item_id=str(o.get("item_id", "")),
                question=str(o.get("question", "")),
                gold_titles=list(o.get("gold_titles", [])),
                primary_title=o.get("primary_title"),
                recall=float(o.get("recall", 0.0)),
                precision=float(o.get("precision", 0.0)),
                f1=float(o.get("f1", 0.0)),
                primary_hit=bool(o.get("primary_hit", False)),
                hit=bool(o.get("hit", False)),
                answer_correctness=o.get("answer_correctness"),
            )
            for o in report.outcomes
        ],
        retrieval_config=dict(report.retrieval_config),
        comparable_reports=list(outcome.comparable_reports),
        superseded_reports=list(outcome.superseded_reports),
        superseded_by=None,
        invalidated_reason=None,
        created_at=report.generated_at,
    )


def _to_report_detail(row: object) -> ReportDetail:
    retrieval_config = dict(getattr(row, "retrieval_config", {}))
    comparable = []
    superseded = []
    if getattr(row, "superseded_by", None) is not None:
        superseded.append(getattr(row, "superseded_by"))
    return ReportDetail(
        id=getattr(row, "id", 0),
        dataset_path=getattr(row, "dataset_path", ""),
        dataset_fingerprint=getattr(row, "dataset_fingerprint", ""),
        content_fingerprint=getattr(row, "content_fingerprint", ""),
        config_fingerprint=getattr(row, "config_fingerprint", ""),
        k=getattr(row, "k", DEFAULT_K),
        questions=getattr(row, "questions", 0),
        evaluator=getattr(row, "evaluator", None),
        metrics=_to_metrics_model(getattr(row, "metrics", {})),
        outcomes=[
            OutcomeModel(
                item_id=str(o.get("item_id", "")),
                question=str(o.get("question", "")),
                gold_titles=list(o.get("gold_titles", [])),
                primary_title=o.get("primary_title"),
                recall=float(o.get("recall", 0.0)),
                precision=float(o.get("precision", 0.0)),
                f1=float(o.get("f1", 0.0)),
                primary_hit=bool(o.get("primary_hit", False)),
                hit=bool(o.get("hit", False)),
                answer_correctness=o.get("answer_correctness"),
            )
            for o in getattr(row, "outcomes", [])
        ],
        retrieval_config=retrieval_config,
        comparable_reports=comparable,
        superseded_reports=superseded,
        superseded_by=getattr(row, "superseded_by", None),
        invalidated_reason=getattr(row, "invalidated_reason", None),
        created_at=(
            getattr(row, "created_at", "").isoformat()
            if hasattr(getattr(row, "created_at", ""), "isoformat")
            else str(getattr(row, "created_at", ""))
        ),
    )