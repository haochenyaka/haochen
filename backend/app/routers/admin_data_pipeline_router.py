"""数据中台管道管理接口：规则 CRUD、管道触发、运行记录查询。

权限：admin/manager。
"""

from __future__ import annotations

import logging
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.data_pipeline_service import (
    DataPipelineService,
    UnknownPipelineTypeError,
)
from app.services.task_dispatch import TaskDispatcher

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/data-pipeline", tags=["admin-data-pipeline"])

pipeline_guard = require_roles("admin", "manager")


def _validate_cron(expr: str | None) -> None:
    """保存前校验 cron 表达式（非法即 422），避免运行期静默跳过。"""
    if not expr:
        return
    from croniter import croniter

    try:
        croniter(expr)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=f"cron 表达式非法：{expr}（{exc}）") from exc


SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(pipeline_guard)]


async def _get_dispatcher(request: Request) -> TaskDispatcher:
    return request.app.state.task_dispatcher


DispatcherDep = Annotated[TaskDispatcher, Depends(_get_dispatcher)]


# ---- Schema ----


class RuleCreate(BaseModel):
    name: str = Field(..., max_length=128)
    description: str | None = None
    task_type: str = Field(..., max_length=64)
    schedule_cron: str | None = None
    config: dict[str, object] | None = None


class RuleUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    description: str | None = None
    schedule_cron: str | None = None
    config: dict[str, object] | None = None
    enabled: bool | None = None


class RuleResponse(BaseModel):
    id: int
    name: str
    description: str | None
    task_type: str
    handler_version: int
    schedule_cron: str | None
    config: dict[str, object] | None
    enabled: bool
    created_at: str
    updated_at: str


class PipelineRunResponse(BaseModel):
    id: str
    rule_id: int | None
    task_id: str | None
    trigger: str
    task_type: str | None
    status: str
    source_table: str | None
    processed_rows: int | None
    affected_rows: int | None
    error_rows: int | None
    duration_seconds: float | None
    summary: dict[str, object] | None
    error: str | None
    created_at: str
    finished_at: str | None


class PipelineStatsResponse(BaseModel):
    total_rules: int
    enabled_rules: int
    total_runs: int
    status_counts: dict[str, int]


class RuleListResponse(BaseModel):
    rules: list[RuleResponse]


class RunListResponse(BaseModel):
    runs: list[PipelineRunResponse]


class TriggerResponse(BaseModel):
    run_id: str


# ---- 规则 CRUD ----


@router.get("/rules", response_model=RuleListResponse)
async def list_rules(
    session: SessionDep,
    _identity: GuardDep,
) -> RuleListResponse:
    """列出所有清洗规则。"""
    svc = DataPipelineService(session)
    rules = await svc.list_rules()
    return RuleListResponse(rules=[_rule_to_response(r) for r in rules])


@router.get("/rules/{rule_id}", response_model=RuleResponse)
async def get_rule(
    rule_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> RuleResponse:
    """获取单条清洗规则。"""
    svc = DataPipelineService(session)
    rule = await svc.get_rule(rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    return _rule_to_response(rule)


@router.post("/rules", response_model=RuleResponse)
async def create_rule(
    body: RuleCreate,
    session: SessionDep,
    _identity: GuardDep,
) -> RuleResponse:
    """创建清洗规则。"""
    _validate_cron(body.schedule_cron)
    svc = DataPipelineService(session)
    try:
        rule = await svc.create_rule(
            name=body.name,
            description=body.description,
            task_type=body.task_type,
            schedule_cron=body.schedule_cron,
            config=body.config,
        )
    except UnknownPipelineTypeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await session.commit()
    return _rule_to_response(rule)


@router.put("/rules/{rule_id}", response_model=RuleResponse)
async def update_rule(
    rule_id: int,
    body: RuleUpdate,
    session: SessionDep,
    _identity: GuardDep,
) -> RuleResponse:
    """更新清洗规则。"""
    _validate_cron(body.schedule_cron)
    svc = DataPipelineService(session)
    rule = await svc.update_rule(
        rule_id,
        name=body.name,
        description=body.description,
        schedule_cron=body.schedule_cron,
        config=body.config,
        enabled=body.enabled,
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    await session.commit()
    return _rule_to_response(rule)


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: int,
    session: SessionDep,
    _identity: GuardDep,
) -> dict[str, bool]:
    """删除清洗规则。"""
    svc = DataPipelineService(session)
    deleted = await svc.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="规则不存在")
    await session.commit()
    return {"deleted": True}


# ---- 管道执行 ----


@router.post("/rules/{rule_id}/trigger", response_model=TriggerResponse)
async def trigger_pipeline(
    rule_id: int,
    session: SessionDep,
    _identity: GuardDep,
    dispatcher: DispatcherDep,
) -> TriggerResponse:
    """触发规则对应的管道。"""
    svc = DataPipelineService(session)
    run = await svc.trigger_pipeline(rule_id, trigger="manual", dispatcher=dispatcher)
    if run is None:
        raise HTTPException(status_code=400, detail="规则不存在、已禁用，或已有运行中的同规则管道")
    return TriggerResponse(run_id=str(run.id))


@router.post("/trigger/{task_type}", response_model=TriggerResponse)
async def trigger_pipeline_task(
    task_type: str,
    session: SessionDep,
    _identity: GuardDep,
    dispatcher: DispatcherDep,
) -> TriggerResponse:
    """直接按 task_type 触发管道（不关联规则）。"""
    svc = DataPipelineService(session)
    run = await svc.trigger_pipeline_once(
        task_type, config=None, trigger="api", dispatcher=dispatcher
    )
    if run is None:
        raise HTTPException(status_code=400, detail="管道任务已在队列中，且同一分钟不重复触发")
    return TriggerResponse(run_id=str(run.id))


# ---- 运行记录 ----


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    session: SessionDep,
    _identity: GuardDep,
    rule_id: int | None = None,
    status: str | None = None,
    limit: int = 20,
) -> RunListResponse:
    """列出管道运行记录。"""
    svc = DataPipelineService(session)
    runs = await svc.list_runs(rule_id=rule_id, status=status, limit=limit)
    return RunListResponse(runs=[_run_to_response(r) for r in runs])


@router.get("/runs/{run_id}", response_model=PipelineRunResponse)
async def get_run(
    run_id: UUID,
    session: SessionDep,
    _identity: GuardDep,
) -> PipelineRunResponse:
    """获取单条运行记录。"""
    svc = DataPipelineService(session)
    run = await svc.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return _run_to_response(run)


# ---- 统计 ----


@router.get("/stats", response_model=PipelineStatsResponse)
async def get_pipeline_stats(
    session: SessionDep,
    _identity: GuardDep,
) -> PipelineStatsResponse:
    """获取数据管道统计概览。"""
    svc = DataPipelineService(session)
    stats = await svc.get_stats()
    return PipelineStatsResponse(**stats)


# ---- 辅助转换 ----


def _rule_to_response(rule: object) -> RuleResponse:
    from app.models.data_pipeline import DataCleaningRule

    r = cast(DataCleaningRule, rule)
    return RuleResponse(
        id=r.id,
        name=r.name,
        description=r.description,
        task_type=r.task_type,
        handler_version=r.handler_version,
        schedule_cron=r.schedule_cron,
        config=r.config,
        enabled=r.enabled,
        created_at=r.created_at.isoformat(),
        updated_at=r.updated_at.isoformat(),
    )


def _run_to_response(run: object) -> PipelineRunResponse:
    from app.models.data_pipeline import DataPipelineRun

    r = cast(DataPipelineRun, run)
    return PipelineRunResponse(
        id=str(r.id),
        rule_id=r.rule_id,
        task_id=str(r.task_id) if r.task_id else None,
        trigger=r.trigger,
        task_type=r.task_type,
        status=r.status,
        source_table=r.source_table,
        processed_rows=r.processed_rows,
        affected_rows=r.affected_rows,
        error_rows=r.error_rows,
        duration_seconds=r.duration_seconds,
        summary=r.summary,
        error=r.error,
        created_at=r.created_at.isoformat(),
        finished_at=r.finished_at.isoformat() if r.finished_at else None,
    )
