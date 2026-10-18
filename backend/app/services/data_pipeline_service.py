"""数据管道编排服务：规则管理、管道触发、运行记录查询。"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, delete, desc, select, update
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_pipeline import DataCleaningRule, DataPipelineRun
from app.services.task_dispatch import TaskDispatcher, dispatch_task
from app.services.task_registry import registered_handlers
from app.services.task_service import TaskService

logger = logging.getLogger(__name__)

PIPELINE_TASK_PREFIX = "data_pipeline."


class UnknownPipelineTypeError(ValueError):
    """未知管道 task_type（未注册的 handler），应显式拒绝。"""


def _registered_pipeline_types() -> frozenset[str]:
    return frozenset(
        task_type
        for (task_type, _version) in registered_handlers()
        if task_type.startswith(PIPELINE_TASK_PREFIX)
    )


def validate_task_type(task_type: str) -> str:
    """校验 task_type 是已注册的 data_pipeline handler（fail-closed，白名单）。"""
    known = _registered_pipeline_types()
    if task_type not in known:
        raise UnknownPipelineTypeError(f"未知管道任务类型：{task_type}；已注册：{sorted(known)}")
    return task_type


class DataPipelineService:
    """数据管道服务：规则 CRUD、管道触发、运行记录。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---- 规则 CRUD ----

    async def list_rules(self) -> list[DataCleaningRule]:
        stmt = select(DataCleaningRule).order_by(DataCleaningRule.created_at.desc())
        rows = await self._session.execute(stmt)
        return list(rows.scalars().all())

    async def get_rule(self, rule_id: int) -> DataCleaningRule | None:
        stmt = select(DataCleaningRule).where(DataCleaningRule.id == rule_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create_rule(
        self,
        *,
        name: str,
        description: str | None,
        task_type: str,
        schedule_cron: str | None,
        config: dict[str, object] | None,
    ) -> DataCleaningRule:
        validate_task_type(task_type)
        rule = DataCleaningRule(
            name=name,
            description=description,
            task_type=task_type,
            handler_version=1,
            schedule_cron=schedule_cron,
            config=config,
            enabled=True,
        )
        self._session.add(rule)
        await self._session.flush()
        return rule

    async def update_rule(
        self,
        rule_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        schedule_cron: str | None = None,
        config: dict[str, object] | None = None,
        enabled: bool | None = None,
    ) -> DataCleaningRule | None:
        values: dict[str, object] = {}
        if name is not None:
            values["name"] = name
        if description is not None:
            values["description"] = description
        if schedule_cron is not None:
            values["schedule_cron"] = schedule_cron
        if config is not None:
            values["config"] = config
        if enabled is not None:
            values["enabled"] = enabled
        if not values:
            return await self.get_rule(rule_id)

        stmt = (
            update(DataCleaningRule)
            .where(DataCleaningRule.id == rule_id)
            .values(**values)
            .returning(DataCleaningRule)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return row

    async def delete_rule(self, rule_id: int) -> bool:
        stmt = delete(DataCleaningRule).where(DataCleaningRule.id == rule_id)
        result = cast(CursorResult[Any], await self._session.execute(stmt))
        return result.rowcount > 0

    # ---- 管道触发 ----

    async def _create_run_and_task(
        self,
        *,
        run_id: UUID,
        rule_id: int | None,
        trigger: str,
        task_type: str,
        handler_version: int,
        config: dict[str, object] | None,
        dedupe_key: str | None = None,
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun | None:
        """创建运行记录 + 提交 durable task。

        **竞态修复**：run 行与 task 行在同一事务内落库并**提交**，提交后才投递 ARQ。
        此前先 `add` 后 `submit_task`（后者自持事务、提交即派发），worker 可能在 run 行
        提交前执行 handler，`complete_run` 的 `WHERE status='running'` 匹配 0 行，run 永
        卡 `running`。现在把 task 行并入本 session 提交，投递发生在提交之后。

        `dedupe_key` 为 None 时按 run 维度生成（每次触发必产出新任务）；调度器传入分钟级
        去重键以确保同一分钟只提交一次。命中去重键时**不建 run 行**并返回 None（避免孤儿）。
        """
        payload: dict[str, object] = {
            "pipeline_run_id": str(run_id),
            "rule_id": rule_id,
            "trigger": trigger,
            "config": config or {},
        }
        # task 行并入同一事务（不在此处投递）
        submission = await TaskService(self._session).submit(
            task_type=task_type,
            handler_version=handler_version,
            payload=payload,
            dedupe_key=dedupe_key or f"pipeline:{run_id}",
        )
        if submission.deduplicated:
            # 同键任务已在队列/运行中：复用既有任务，不为本次新建 run 行（避免孤儿 running）
            return None

        pipeline_run = DataPipelineRun(
            id=run_id,
            rule_id=rule_id,
            trigger=trigger,
            task_type=task_type,
            status="running",
            task_id=submission.task_id,
        )
        self._session.add(pipeline_run)
        await self._session.commit()

        # 提交之后才投递：投递失败由 pending 补发恢复，run 行已可见
        if submission.should_dispatch:
            await dispatch_task(dispatcher, submission.task_id)

        return pipeline_run

    async def _has_active_run(self, rule_id: int) -> bool:
        """是否已有 pending/running 的运行（同规则并发互斥的判定）。"""
        stmt = (
            select(sa_func.count())
            .select_from(DataPipelineRun)
            .where(DataPipelineRun.rule_id == rule_id)
            .where(DataPipelineRun.status.in_(("pending", "running")))
        )
        return bool((await self._session.execute(stmt)).scalar() or 0)

    async def trigger_pipeline(
        self,
        rule_id: int,
        *,
        trigger: str = "manual",
        dedupe_key: str | None = None,
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun | None:
        """触发规则对应的清洗管道。

        规则不存在/已禁用、或已有运行中的同规则任务时返回 None（调用方据此拒绝）。
        并发互斥在 DB 层判定：不产生孤儿 run 行（durable task 的去重按 run 维度，不复用）。
        """
        rule = await self.get_rule(rule_id)
        if rule is None or not rule.enabled:
            return None
        if await self._has_active_run(rule_id):
            logger.info("规则已有运行中的管道，拒绝重复触发：rule_id=%s", rule_id)
            return None

        return await self._create_run_and_task(
            run_id=uuid4(),
            rule_id=rule_id,
            trigger=trigger,
            task_type=rule.task_type,
            handler_version=rule.handler_version,
            config=rule.config,
            dedupe_key=dedupe_key,
            dispatcher=dispatcher,
        )

    async def trigger_pipeline_once(
        self,
        task_type: str,
        *,
        config: dict[str, object] | None = None,
        trigger: str = "api",
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun | None:
        """触发一次性管道（不关联规则）。"""
        return await self._create_run_and_task(
            run_id=uuid4(),
            rule_id=None,
            trigger=trigger,
            task_type=task_type,
            handler_version=1,
            config=config,
            dispatcher=dispatcher,
        )

    # ---- 运行记录完成回调（从 task handler 调用） ----

    async def complete_run(
        self,
        run_id: UUID,
        *,
        status: str,
        source_table: str | None = None,
        processed_rows: int = 0,
        affected_rows: int = 0,
        error_rows: int = 0,
        duration_seconds: float = 0,
        summary: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        """更新管道运行结果（由 task handler 在提交事务内调用）。"""
        stmt = (
            update(DataPipelineRun)
            .where(DataPipelineRun.id == run_id)
            .where(DataPipelineRun.status == "running")
            .values(
                status=status,
                source_table=source_table,
                processed_rows=processed_rows,
                affected_rows=affected_rows,
                error_rows=error_rows,
                duration_seconds=duration_seconds,
                summary=summary,
                error=error,
                finished_at=sa_func.now(),
            )
        )
        await self._session.execute(stmt)

    async def fail_run(
        self,
        run_id: UUID,
        *,
        source_table: str | None = None,
        error: str | None = None,
    ) -> None:
        """收敛未由 handler 回报的运行记录（如 worker 租约过期）。

        只更新 pending/running 行；handler 已自行置为 failed 时不会覆盖其结果。
        """
        stmt = (
            update(DataPipelineRun)
            .where(DataPipelineRun.id == run_id)
            .where(DataPipelineRun.status.in_(("pending", "running")))
            .values(
                status="failed",
                source_table=source_table,
                error=error,
                duration_seconds=sa_func.extract(
                    "epoch", sa_func.now() - DataPipelineRun.created_at
                ),
                finished_at=sa_func.now(),
            )
        )
        await self._session.execute(stmt)

    # ---- 运行记录查询 ----

    async def list_runs(
        self,
        rule_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[DataPipelineRun]:
        stmt = select(DataPipelineRun)
        if rule_id is not None:
            stmt = stmt.where(DataPipelineRun.rule_id == rule_id)
        if status is not None:
            stmt = stmt.where(DataPipelineRun.status == status)
        stmt = stmt.order_by(desc(DataPipelineRun.created_at)).limit(limit)
        rows = await self._session.execute(stmt)
        return list(rows.scalars().all())

    async def get_run(self, run_id: UUID) -> DataPipelineRun | None:
        stmt = select(DataPipelineRun).where(DataPipelineRun.id == run_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_stats(self) -> dict[str, Any]:
        """获取管道统计概览。"""
        rows = (
            await self._session.execute(
                select(
                    DataPipelineRun.status,
                    sa_func.count(DataPipelineRun.id),
                ).group_by(DataPipelineRun.status)
            )
        ).all()
        status_counts: dict[str, int] = {r[0]: r[1] for r in rows}
        return {
            "total_rules": (
                await self._session.execute(select(sa_func.count(DataCleaningRule.id)))
            ).scalar()
            or 0,
            "enabled_rules": (
                await self._session.execute(
                    select(sa_func.count(DataCleaningRule.id)).where(
                        DataCleaningRule.enabled.is_(True)
                    )
                )
            ).scalar()
            or 0,
            "total_runs": sum(status_counts.values()),
            "status_counts": status_counts,
        }
