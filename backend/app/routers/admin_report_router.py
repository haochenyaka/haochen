"""运营报表接口：一切数值由 PostgreSQL 审计事实只读派生（验收主张 A4）。

权限：admin/manager 只读（术语表 §11 P7 权限矩阵）。时间窗为半开区间 [start, end)，
缺省最近 30 天；口径见术语表 §15。本路由不写任何统计副本，报表数值恒等于对审计表重算。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.report_service import ReportService

router = APIRouter(prefix="/admin/reports", tags=["admin-reports"])

report_guard = require_roles("admin", "manager")

DEFAULT_REPORT_DAYS = 30
MAX_REPORT_DAYS = 366


class ReportWindow(BaseModel):
    start: datetime
    end: datetime


async def resolve_window(
    start: Annotated[datetime | None, Query(description="起始（含），ISO8601")] = None,
    end: Annotated[datetime | None, Query(description="结束（不含），ISO8601")] = None,
) -> ReportWindow:
    """解析并校验时间窗：naive 时间按 UTC 处理，start 必须早于 end 且跨度受限。"""
    resolved_end = _aware(end) if end is not None else datetime.now(UTC)
    resolved_start = _aware(start) if start is not None else resolved_end - timedelta(
        days=DEFAULT_REPORT_DAYS
    )
    if resolved_start >= resolved_end:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="start 必须早于 end"
        )
    if resolved_end - resolved_start > timedelta(days=MAX_REPORT_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"时间窗跨度不得超过 {MAX_REPORT_DAYS} 天",
        )
    return ReportWindow(start=resolved_start, end=resolved_end)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


WindowDep = Annotated[ReportWindow, Depends(resolve_window)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(report_guard)]


class ReceptionModel(BaseModel):
    threads: int
    runs: int
    messages: int


class ResolutionModel(BaseModel):
    terminal_runs: int
    first_resolved_runs: int
    first_resolution_rate: float | None
    tasks_succeeded: int
    tasks_failed: int
    task_success_rate: float | None


class HandoffRateModel(BaseModel):
    handoffs: int
    threads: int
    rate: float | None


class RecallModel(BaseModel):
    offline_recall_at_k: float | None
    offline_k: int | None
    offline_report_id: int | None
    online_runs_with_retrieval: int
    online_runs_cited: int
    online_citation_rate: float | None


class CacheHitModel(BaseModel):
    prompt_tokens: int
    cached_prompt_tokens: int
    hit_rate: float | None
    estimated_saving: float


class PerformanceModel(BaseModel):
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    ttft_samples: int
    tool_calls: int
    tool_completed: int
    tool_success_rate: float | None
    runs_with_attempts: int
    runs_retried: int
    model_retry_rate: float | None


class OverviewResponse(BaseModel):
    window: ReportWindow
    reception: ReceptionModel
    resolution: ResolutionModel
    handoff: HandoffRateModel
    recall: RecallModel
    cache: CacheHitModel
    performance: PerformanceModel


class TokenTotalsModel(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int


class TokenBucketModel(BaseModel):
    key: str
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int


class TokensResponse(BaseModel):
    window: ReportWindow
    totals: TokenTotalsModel
    by_model: list[TokenBucketModel]
    by_agent: list[TokenBucketModel]
    by_day: list[TokenBucketModel]


class NameCount(BaseModel):
    name: str
    count: int


class HotQuestionsResponse(BaseModel):
    window: ReportWindow
    top_tools: list[NameCount]
    top_keywords: list[NameCount]


class DailyCountModel(BaseModel):
    day: date
    threads: int
    runs: int
    messages: int


class ReceptionSeriesResponse(BaseModel):
    window: ReportWindow
    series: list[DailyCountModel]


@router.get("/overview", response_model=OverviewResponse)
async def get_overview(
    session: SessionDep, window: WindowDep, _identity: GuardDep
) -> OverviewResponse:
    svc = ReportService(session)
    start, end = window.start, window.end
    return OverviewResponse(
        window=window,
        reception=ReceptionModel.model_validate(
            await svc.reception(start=start, end=end), from_attributes=True
        ),
        resolution=ResolutionModel.model_validate(
            await svc.resolution(start=start, end=end), from_attributes=True
        ),
        handoff=HandoffRateModel.model_validate(
            await svc.handoff_rate(start=start, end=end), from_attributes=True
        ),
        recall=RecallModel.model_validate(
            await svc.recall(start=start, end=end), from_attributes=True
        ),
        cache=CacheHitModel.model_validate(
            await svc.cache_hit(start=start, end=end), from_attributes=True
        ),
        performance=PerformanceModel.model_validate(
            await svc.performance(start=start, end=end), from_attributes=True
        ),
    )


@router.get("/tokens", response_model=TokensResponse)
async def get_tokens(session: SessionDep, window: WindowDep, _identity: GuardDep) -> TokensResponse:
    svc = ReportService(session)
    start, end = window.start, window.end
    totals = await svc.token_totals(start=start, end=end)
    return TokensResponse(
        window=window,
        totals=TokenTotalsModel(
            prompt_tokens=totals.prompt_tokens,
            completion_tokens=totals.completion_tokens,
            cached_prompt_tokens=totals.cached_prompt_tokens,
        ),
        by_model=[
            TokenBucketModel.model_validate(bucket, from_attributes=True)
            for bucket in await svc.token_usage_by_model(start=start, end=end)
        ],
        by_agent=[
            TokenBucketModel.model_validate(bucket, from_attributes=True)
            for bucket in await svc.token_usage_by_agent(start=start, end=end)
        ],
        by_day=[
            TokenBucketModel.model_validate(bucket, from_attributes=True)
            for bucket in await svc.token_usage_by_day(start=start, end=end)
        ],
    )


@router.get("/hot-questions", response_model=HotQuestionsResponse)
async def get_hot_questions(
    session: SessionDep,
    window: WindowDep,
    _identity: GuardDep,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> HotQuestionsResponse:
    svc = ReportService(session)
    hot = await svc.hot_questions(start=window.start, end=window.end, limit=limit)
    return HotQuestionsResponse(
        window=window,
        top_tools=[NameCount(name=name, count=count) for name, count in hot.top_tools],
        top_keywords=[NameCount(name=name, count=count) for name, count in hot.top_keywords],
    )


@router.get("/reception-series", response_model=ReceptionSeriesResponse)
async def get_reception_series(
    session: SessionDep, window: WindowDep, _identity: GuardDep
) -> ReceptionSeriesResponse:
    svc = ReportService(session)
    series = await svc.reception_series(start=window.start, end=window.end)
    return ReceptionSeriesResponse(
        window=window,
        series=[DailyCountModel.model_validate(item, from_attributes=True) for item in series],
    )
