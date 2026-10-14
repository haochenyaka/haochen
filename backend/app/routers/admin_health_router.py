"""运行健康面板接口：Run 终态分布、首 token 延迟、工具成功率、interrupt 比例、
模型重试率、缓存命中率趋势。一切数值由 ReportService 从审计事实派生（验收主张 A4）。

权限：admin/manager 只读。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.report_service import ReportService

router = APIRouter(prefix="/admin/health", tags=["admin-health"])

guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(guard)]

DEFAULT_REPORT_DAYS = 30
MAX_REPORT_DAYS = 366


class ReportWindow(BaseModel):
    start: datetime
    end: datetime


async def resolve_window(
    start: Annotated[datetime | None, Query(description="起始（含），ISO8601")] = None,
    end: Annotated[datetime | None, Query(description="结束（不含），ISO8601")] = None,
) -> ReportWindow:
    resolved_end = _aware(end) if end is not None else datetime.now(UTC)
    resolved_start = (
        _aware(start) if start is not None else resolved_end - timedelta(days=DEFAULT_REPORT_DAYS)
    )
    if resolved_start >= resolved_end:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="start 必须早于 end")
    if resolved_end - resolved_start > timedelta(days=MAX_REPORT_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"时间窗跨度不得超过 {MAX_REPORT_DAYS} 天",
        )
    return ReportWindow(start=resolved_start, end=resolved_end)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


WindowDep = Annotated[ReportWindow, Depends(resolve_window)]


class RunStatusDistributionModel(BaseModel):
    counts: dict[str, int]
    total: int
    interrupted: int
    interrupt_ratio: float | None


class CacheHitTrendModel(BaseModel):
    prompt_tokens: int
    cached_prompt_tokens: int
    hit_rate: float | None
    estimated_saving: float


@router.get("/run-status-distribution", response_model=RunStatusDistributionModel)
async def get_run_status_distribution(
    session: SessionDep,
    _identity: GuardDep,
    start: Annotated[datetime | None, Query(description="起始（含），ISO8601")] = None,
    end: Annotated[datetime | None, Query(description="结束（不含），ISO8601")] = None,
) -> RunStatusDistributionModel:
    svc = ReportService(session)
    resolved_end = _aware(end) if end is not None else datetime.now()
    resolved_start = _aware(start) if start is not None else resolved_end
    dist = await svc.run_status_distribution(start=resolved_start, end=resolved_end)
    return RunStatusDistributionModel(
        counts=dist.counts,
        total=dist.total,
        interrupted=dist.interrupted,
        interrupt_ratio=dist.interrupt_ratio,
    )


@router.get("/cache-hit-trend", response_model=list[CacheHitTrendModel])
async def get_cache_hit_trend(
    session: SessionDep,
    _identity: GuardDep,
    window: WindowDep,
) -> list[CacheHitTrendModel]:
    svc = ReportService(session)
    series = await svc.cache_hit_series(start=window.start, end=window.end)
    return [CacheHitTrendModel.model_validate(item, from_attributes=True) for item in series]
