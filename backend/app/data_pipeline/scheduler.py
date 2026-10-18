"""数据管道调度器：按规则的 `schedule_cron` 提交 durable task。

机制（PR-D3）：
- 由 task-worker 的 ARQ cron 每分钟调用 `submit_pipeline_schedules_job`；
- 扫描 `data_cleaning_rules`，用 `croniter` 判定该分钟是否命中 `schedule_cron`；
- 命中且启用的规则 → `DataPipelineService.trigger_pipeline(trigger="scheduled")`；
- 并发/重启去重由「同规则已有运行中 run 则拒绝」（PR-D1）与分钟级水位共同保证。

`schedule_cron` 此前全仓没有任何读取点——填了也不生效。本模块让它真正生效。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from croniter import croniter

from app.services.data_pipeline_service import DataPipelineService
from app.services.run_worker import context_session_factory
from app.services.task_service import context_dispatcher

logger = logging.getLogger(__name__)

PIPELINE_SCHEDULE_DEDUPE_PREFIX = "pipeline:schedule"


def schedule_dedupe_key(rule_id: int, minute: datetime) -> str:
    """分钟级去重键：同一规则同一分钟只调度一次（多副本/重启均安全）。"""
    bucket = minute.astimezone(UTC).strftime("%Y%m%d%H%M")
    return f"{PIPELINE_SCHEDULE_DEDUPE_PREFIX}:{rule_id}:{bucket}"


def is_due(cron_expr: str, now: datetime) -> bool:
    """`now` 所在分钟是否命中 cron 表达式。非法表达式返回 False（不炸整轮扫描）。"""
    try:
        base = now.replace(second=0, microsecond=0)
        return bool(croniter.match(cron_expr, base))
    except (ValueError, KeyError):
        logger.warning("非法 cron 表达式，跳过：%s", cron_expr)
        return False


async def submit_due_pipelines(
    *,
    now: datetime | None = None,
    session_factory: Any = None,
    dispatcher: Any = None,
) -> list[UUID]:
    """扫描规则并提交命中的管道，返回新提交的 run_id 列表。"""
    from app.db import SessionFactory

    factory = session_factory or SessionFactory
    moment = now or datetime.now(UTC)
    submitted: list[UUID] = []

    async with factory() as session:
        rules = await DataPipelineService(session).list_rules()

    for rule in rules:
        if not rule.enabled or not rule.schedule_cron:
            continue
        if not is_due(rule.schedule_cron, moment):
            continue

        dedupe_key = schedule_dedupe_key(rule.id, moment)
        async with factory() as session:
            svc = DataPipelineService(session)
            # trigger_pipeline 内部已做「同规则已有运行中 run 则拒绝」的并发互斥；
            # 分钟级去重键保证同一分钟只提交一次（多副本/重启安全）。
            run = await svc.trigger_pipeline(
                rule.id, trigger="scheduled", dedupe_key=dedupe_key, dispatcher=dispatcher
            )
            if run is not None:
                submitted.append(run.id)
                logger.info(
                    "已调度数据管道：rule_id=%s run_id=%s cron=%s",
                    rule.id,
                    run.id,
                    rule.schedule_cron,
                )
    return submitted


async def submit_pipeline_schedules_job(ctx: dict[str, Any]) -> list[str]:
    """ARQ cron 入口：每分钟扫描命中的清洗规则并提交。"""
    factory = context_session_factory(ctx)
    dispatcher = context_dispatcher(ctx)
    run_ids = await submit_due_pipelines(session_factory=factory, dispatcher=dispatcher)
    return [str(run_id) for run_id in run_ids]


__all__ = [
    "PIPELINE_SCHEDULE_DEDUPE_PREFIX",
    "is_due",
    "schedule_dedupe_key",
    "submit_due_pipelines",
    "submit_pipeline_schedules_job",
]
