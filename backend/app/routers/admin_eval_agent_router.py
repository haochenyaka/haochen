"""智能体评估接口：列出可用数据集、触发实验、查询/作废报告。

权限：admin/manager。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.eval.agent.dataset import EvalSetError, dataset_fingerprint, load_eval_set
from app.eval.agent.executor import RealCaseExecutor
from app.eval.agent.runner import (
    DEFAULT_DATASET_DIR,
    EvalRunError,
    persist_report,
    run_eval,
)
from app.repositories.eval_agent_repo import AgentEvalReportRepository
from app.security import Identity, require_roles

router = APIRouter(prefix="/admin/eval/agent", tags=["admin-eval-agent"])

eval_guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(eval_guard)]


class DatasetInfo(BaseModel):
    path: str
    name: str
    description: str
    case_count: int
    fingerprint: str


class RunRequest(BaseModel):
    dataset_path: str


class OutcomeModel(BaseModel):
    item_id: str
    name: str
    tool_accuracy: float
    task_success: bool | None
    tags: list[str]
    answer: str | None = None
    judge: dict[str, Any] | None = None
    judge_score: float | None = None


class MetricsModel(BaseModel):
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


class ReportItem(BaseModel):
    id: int
    dataset_path: str
    dataset_fingerprint: str
    config_fingerprint: str
    model: str | None
    total_cases: int
    metrics: MetricsModel
    superseded_by: int | None
    invalidated_reason: str | None
    created_at: str


class ReportDetail(ReportItem):
    outcomes: list[OutcomeModel]


@router.get("/datasets", response_model=list[DatasetInfo])
async def list_datasets(_identity: GuardDep) -> list[DatasetInfo]:
    """列出可用评估数据集。"""
    if not DEFAULT_DATASET_DIR.is_dir():
        return []
    results: list[DatasetInfo] = []
    for fpath in sorted(DEFAULT_DATASET_DIR.glob("*.yaml")):
        try:
            items = load_eval_set(fpath)
            fp = dataset_fingerprint(items)
            results.append(
                DatasetInfo(
                    path=str(fpath),
                    name=fpath.stem,
                    description=_read_description(fpath),
                    case_count=len(items),
                    fingerprint=fp,
                )
            )
        except Exception:
            continue
    return results


@router.post("/run", response_model=ReportDetail)
async def run_agent_eval(
    body: RunRequest,
    session: SessionDep,
    _identity: GuardDep,
) -> ReportDetail:
    """触发智能体评估实验：逐条跑真实编译图，工具序列从审计事实回读。

    未配置模型密钥时返回 400（工具序列由模型产生，缺密钥只会得到恒零分数）；
    用例会真实落库（线程/消息/审计以及订单、任务等业务副作用）。
    """
    dataset_path = Path(body.dataset_path)
    if not dataset_path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"数据集不存在：{dataset_path}",
        )
    try:
        load_eval_set(dataset_path)
    except EvalSetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    try:
        report = await run_eval(session, dataset_path=dataset_path, executor=RealCaseExecutor())
    except EvalRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    report = await persist_report(session, report)
    return _to_report_detail(report)


@router.get("/reports", response_model=list[ReportItem])
async def list_reports(
    session: SessionDep,
    _identity: GuardDep,
    limit: int = 20,
) -> list[ReportItem]:
    """列出最近评估报告。"""
    repo = AgentEvalReportRepository(session)
    rows = await repo.list_recent(limit=limit)
    return [
        ReportItem(
            id=row.id,
            dataset_path=row.dataset_path,
            dataset_fingerprint=row.dataset_fingerprint,
            config_fingerprint=row.config_fingerprint,
            model=row.model,
            total_cases=row.total_cases,
            metrics=MetricsModel(**row.metrics),
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
    repo = AgentEvalReportRepository(session)
    row = await repo.get(report_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    return _to_report_detail_from_orm(row)


@router.post("/reports/{report_id}/invalidate", response_model=ReportItem)
async def invalidate_report(
    report_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> ReportItem:
    """手动作废报告。"""
    repo = AgentEvalReportRepository(session)
    row = await repo.get(report_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="报告不存在")
    await repo.mark_superseded(report_id, superseded_by=report_id, reason="manual_invalidation")
    await session.commit()
    # re-read to get updated state
    row = await repo.get(report_id)
    assert row is not None
    return ReportItem(
        id=row.id,
        dataset_path=row.dataset_path,
        dataset_fingerprint=row.dataset_fingerprint,
        config_fingerprint=row.config_fingerprint,
        model=row.model,
        total_cases=row.total_cases,
        metrics=MetricsModel(**row.metrics),
        superseded_by=row.superseded_by,
        invalidated_reason=row.invalidated_reason,
        created_at=row.created_at.isoformat(),
    )


def _read_description(fpath: Path) -> str:
    """从 YAML 头部读取描述。"""
    import yaml

    raw = yaml.safe_load(fpath.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        meta = raw.get("fingerprint_meta", {})
        if isinstance(meta, dict):
            return str(meta.get("description", ""))
    return ""


def _to_report_detail(report: object) -> ReportDetail:
    from app.eval.agent.runner import AgentEvalReportData

    if isinstance(report, AgentEvalReportData):
        outcomes = report.outcomes
        metrics = report.metrics
    else:
        # fallback for ORM model
        outcomes = getattr(report, "outcomes", [])
        metrics = getattr(report, "metrics", {})
    return _build_detail(
        report_id=getattr(report, "report_id", None) or 0,
        dataset_path=getattr(report, "dataset_path", ""),
        dataset_fingerprint=getattr(report, "dataset_fingerprint", ""),
        config_fingerprint=getattr(report, "config_fingerprint", ""),
        model=getattr(report, "model", None),
        total_cases=int(metrics.get("questions", len(outcomes))),
        metrics=metrics,
        outcomes=outcomes,
        superseded_by=None,
        invalidated_reason=None,
        created_at=getattr(report, "generated_at", ""),
    )


def _to_report_detail_from_orm(row: object) -> ReportDetail:
    metrics_dict = getattr(row, "metrics", {})
    outcomes_list = getattr(row, "outcomes", [])
    created = getattr(row, "created_at", "")
    return _build_detail(
        report_id=getattr(row, "id", 0),
        dataset_path=getattr(row, "dataset_path", ""),
        dataset_fingerprint=getattr(row, "dataset_fingerprint", ""),
        config_fingerprint=getattr(row, "config_fingerprint", ""),
        model=getattr(row, "model", None),
        total_cases=getattr(row, "total_cases", 0),
        metrics=metrics_dict,
        outcomes=outcomes_list,
        superseded_by=getattr(row, "superseded_by", None),
        invalidated_reason=getattr(row, "invalidated_reason", None),
        created_at=created.isoformat() if hasattr(created, "isoformat") else str(created),
    )


def _build_detail(
    *,
    report_id: int,
    dataset_path: str,
    dataset_fingerprint: str,
    config_fingerprint: str,
    model: str | None,
    total_cases: int,
    metrics: dict[str, Any],
    outcomes: list[dict[str, Any]],
    superseded_by: int | None,
    invalidated_reason: str | None,
    created_at: str,
) -> ReportDetail:
    return ReportDetail(
        id=report_id,
        dataset_path=dataset_path,
        dataset_fingerprint=dataset_fingerprint,
        config_fingerprint=config_fingerprint,
        model=model,
        total_cases=total_cases,
        metrics=MetricsModel(
            questions=metrics.get("questions", 0),
            avg_tool_accuracy=float(metrics.get("avg_tool_accuracy", 0.0)),
            avg_args_match_rate=float(metrics.get("avg_args_match_rate", 0.0)),
            task_success_rate=metrics.get("task_success_rate"),
            total_passed=metrics.get("total_passed", 0),
            by_tag=metrics.get("by_tag", {}),
            avg_judge_score=metrics.get("avg_judge_score"),
            judge_scored=metrics.get("judge_scored", 0),
            judge_pass_rate=metrics.get("judge_pass_rate"),
            judge_dimensions=metrics.get("judge_dimensions"),
        ),
        outcomes=[
            OutcomeModel(
                item_id=o.get("item_id", ""),
                name=o.get("name", ""),
                tool_accuracy=float(o.get("tool_accuracy", 0.0)),
                task_success=o.get("task_success"),
                tags=o.get("tags", []),
                answer=o.get("answer"),
                judge=o.get("judge"),
                judge_score=o.get("judge_score"),
            )
            for o in outcomes
        ],
        superseded_by=superseded_by,
        invalidated_reason=invalidated_reason,
        created_at=created_at,
    )
