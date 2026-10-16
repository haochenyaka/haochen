"""质检跑批任务：`qa.audit` v1 —— 扫描增量会话产出自动标注与质量信号。

触发：task worker 的小时级 cron（小时级去重键，同一小时至多一次）与管理端同步触发
（`GET /admin/quality/audit`，不建任务，直接跑一轮）。本任务无外部副作用，标注自身幂等
（同线程同标签跳过），故不自动重试也不重放——失败由下一次调度补齐。

参数从管理端 qa-auditor 配置读取（扫描窗口 / 过期天数 / 低召回阈值），未配置取代码默认。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.buildin.qa_auditor.agent import QaAuditorAgent, QaAuditorContext
from app.db import SessionFactory
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.repositories import TaskRepository
from app.services.agent_config_service import AgentConfigService
from app.services.qa_auditor_service import QaAuditorService
from app.services.task_dispatch import TaskDispatcher, dispatch_task
from app.services.task_registry import TaskContext, task_handler
from app.services.task_service import (
    TaskService,
    TaskSubmission,
    context_dispatcher,
    context_session_factory,
)

logger = logging.getLogger(__name__)

QA_AUDIT_TASK_TYPE = "qa.audit"
QA_AUDIT_HANDLER_VERSION = 1
QA_AUDIT_DEDUPE_PREFIX = "qa.audit:"


def qa_audit_dedupe_key(moment: datetime) -> str:
    """小时级去重键（酒店本地时区）：同一小时至多提交一次，重启补发与手动触发都不堆积。"""
    return f"{QA_AUDIT_DEDUPE_PREFIX}{moment.astimezone(HOTEL_TIMEZONE).strftime('%Y%m%dT%H')}"


@task_handler(QA_AUDIT_TASK_TYPE, QA_AUDIT_HANDLER_VERSION)
async def audit_quality(ctx: TaskContext) -> Mapping[str, Any]:
    """跑一轮质检：自动标注 + 质量信号；返回元数据（计数），明细在各自表与检测结果里。"""
    async with ctx.session_factory() as session:
        runtime_config = await AgentConfigService(session).load_runtime_config(QaAuditorAgent.slug)
        context = QaAuditorContext.from_config(runtime_config.context)
        service = QaAuditorService(
            session,
            scan_window_hours=context.scan_window_hours,
            stale_days=context.stale_days,
            low_recall_threshold=context.low_recall_threshold,
        )
        labels_created = await service.auto_label_recent_threads()
        stale_documents = await service.detect_stale_documents()
        coverage_gaps = await service.compute_coverage_gaps()
        low_recall_kbs = await service.detect_low_recall_kbs()
        await session.commit()

    logger.info(
        "qa.audit 完成：labels=%d stale=%d gaps=%d low_recall=%d",
        labels_created,
        len(stale_documents),
        len(coverage_gaps),
        len(low_recall_kbs),
    )
    return {
        "labels_created": labels_created,
        "stale_documents": len(stale_documents),
        "coverage_gaps": len(coverage_gaps),
        "low_recall_kbs": len(low_recall_kbs),
    }


async def submit_qa_audit(
    *,
    dedupe_key: str | None = None,
    dispatcher: TaskDispatcher | None = None,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> TaskSubmission:
    """自持事务的提交入口：落库提交后投递（投递失败保留 pending 由恢复补发）。"""
    async with session_factory() as session:
        submission = await TaskService(session).submit(
            task_type=QA_AUDIT_TASK_TYPE,
            handler_version=QA_AUDIT_HANDLER_VERSION,
            payload={},
            dedupe_key=dedupe_key,
        )
        await session.commit()
    if submission.should_dispatch:
        await dispatch_task(dispatcher, submission.task_id)
    return submission


async def submit_qa_audit_job(ctx: dict[str, Any]) -> str | None:
    """cron 入口：拿到本小时去重键后提交一次质检跑批（已提交过则跳过）。"""
    factory = context_session_factory(ctx)
    dedupe_key = qa_audit_dedupe_key(datetime.now(HOTEL_TIMEZONE))
    async with factory() as session:
        if await TaskRepository(session).exists_by_dedupe_key(dedupe_key):
            return None
    submission = await submit_qa_audit(
        dedupe_key=dedupe_key,
        dispatcher=context_dispatcher(ctx),
        session_factory=factory,
    )
    logger.info(
        "qa.audit 已提交：key=%s task_id=%s deduplicated=%s",
        dedupe_key,
        submission.task_id,
        submission.deduplicated,
    )
    return str(submission.task_id)


__all__ = [
    "QA_AUDIT_DEDUPE_PREFIX",
    "QA_AUDIT_HANDLER_VERSION",
    "QA_AUDIT_TASK_TYPE",
    "audit_quality",
    "qa_audit_dedupe_key",
    "submit_qa_audit",
    "submit_qa_audit_job",
]
