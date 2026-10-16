"""数据质量评分卡接口：查看/触发评分卡，数据质量治理。

权限：admin/manager。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models.quality_scorecard import QualityScorecard
from app.security import Identity, require_roles
from app.services.qa_auditor_service import QaAuditorService
from app.services.quality_service import QualityScore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/quality", tags=["admin-quality"])

quality_guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(quality_guard)]


class DimensionDetail(BaseModel):
    score: float
    count: int
    actionable: int
    detail: dict[str, object] | None = None


class ScorecardResponse(BaseModel):
    id: int
    generated_by: str
    coverage: DimensionDetail
    freshness: DimensionDetail
    correctness: DimensionDetail
    consistency: DimensionDetail
    recall_quality: DimensionDetail
    overall_score: float
    summary: str | None
    latest_kb_eval_report_id: int | None
    latest_agent_eval_report_id: int | None
    created_at: str


class ScorecardItem(BaseModel):
    id: int
    overall_score: float
    generated_by: str
    summary: str | None
    created_at: str


class QaAuditResult(BaseModel):
    labels_created: int
    stale_documents: list[dict[str, object]]
    coverage_gaps: list[dict[str, object]]
    low_recall_kbs: list[dict[str, object]]


@router.get("/scorecards/latest", response_model=ScorecardResponse | None)
async def get_latest_scorecard(
    session: SessionDep,
    _identity: GuardDep,
) -> ScorecardResponse | None:
    """获取最新评分卡。"""
    from sqlalchemy import select

    stmt = select(QualityScorecard).order_by(QualityScorecard.created_at.desc()).limit(1)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return None
    return _to_response(row)


@router.post("/scorecards/compute", response_model=ScorecardResponse)
async def compute_scorecard(
    session: SessionDep,
    _identity: GuardDep,
) -> ScorecardResponse:
    """触发数据质量评分卡计算。"""
    builder = QualityScore(session)
    scorecard = await builder.compute(generated_by="manual")
    await session.commit()
    return _to_response(scorecard)


@router.get("/scorecards", response_model=list[ScorecardItem])
async def list_scorecards(
    session: SessionDep,
    _identity: GuardDep,
    limit: int = 10,
) -> list[ScorecardItem]:
    """列出历史评分卡。"""
    from sqlalchemy import select

    stmt = select(QualityScorecard).order_by(QualityScorecard.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        ScorecardItem(
            id=row.id,
            overall_score=row.overall_score,
            generated_by=row.generated_by,
            summary=row.summary,
            created_at=row.created_at.isoformat(),
        )
        for row in rows
    ]


@router.get("/audit", response_model=QaAuditResult)
async def run_qa_audit(
    session: SessionDep,
    _identity: GuardDep,
) -> QaAuditResult:
    """执行 QA 自动标注扫描（只读检测+标注，不计算完整评分卡）。"""
    qa = QaAuditorService(session)
    labels_created = await qa.auto_label_recent_threads()
    stale = await qa.detect_stale_documents()
    gaps = await qa.compute_coverage_gaps()
    low_recall = await qa.detect_low_recall_kbs()
    await session.commit()
    return QaAuditResult(
        labels_created=labels_created,
        stale_documents=stale,
        coverage_gaps=gaps,
        low_recall_kbs=low_recall,
    )


def _to_response(row: QualityScorecard) -> ScorecardResponse:
    return ScorecardResponse(
        id=row.id,
        generated_by=row.generated_by,
        coverage=DimensionDetail(
            score=row.coverage_score,
            count=row.coverage_gaps,
            actionable=row.coverage_actionable,
            detail=row.coverage_detail,
        ),
        freshness=DimensionDetail(
            score=row.freshness_score,
            count=row.freshness_expired,
            actionable=row.freshness_actionable,
            detail=row.freshness_detail,
        ),
        correctness=DimensionDetail(
            score=row.correctness_score,
            count=row.correctness_issues,
            actionable=row.correctness_actionable,
            detail=row.correctness_detail,
        ),
        consistency=DimensionDetail(
            score=row.consistency_score,
            count=row.consistency_conflicts,
            actionable=row.consistency_actionable,
            detail=row.consistency_detail,
        ),
        recall_quality=DimensionDetail(
            score=row.recall_quality_score,
            count=row.recall_low_count,
            actionable=row.recall_actionable,
            detail=row.recall_quality_detail,
        ),
        overall_score=row.overall_score,
        summary=row.summary,
        latest_kb_eval_report_id=row.latest_kb_eval_report_id,
        latest_agent_eval_report_id=row.latest_agent_eval_report_id,
        created_at=row.created_at.isoformat(),
    )
