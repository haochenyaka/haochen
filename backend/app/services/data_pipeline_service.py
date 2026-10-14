"""数据管道编排服务：规则管理、管道触发、运行记录查询。"""

from __future__ import annotations

import logging
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, delete, desc, select, update
from sqlalchemy import func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_pipeline import DataCleaningRule, DataPipelineRun
from app.services.task_dispatch import TaskDispatcher
from app.services.task_service import submit_task

logger = logging.getLogger(__name__)


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
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun:
        """创建运行记录 + 提交 durable task（先写库后 dispatch）。"""
        pipeline_run = DataPipelineRun(
            id=run_id,
            rule_id=rule_id,
            trigger=trigger,
            status="running",
        )
        self._session.add(pipeline_run)

        payload: dict[str, object] = {
            "pipeline_run_id": str(run_id),
            "rule_id": rule_id,
            "trigger": trigger,
            "config": config or {},
        }
        submission = await submit_task(
            task_type=task_type,
            handler_version=handler_version,
            payload=payload,
            dedupe_key=None,
            dispatcher=dispatcher,
        )
        pipeline_run.task_id = submission.task_id
        await self._session.flush()
        return pipeline_run

    async def trigger_pipeline(
        self,
        rule_id: int,
        *,
        trigger: str = "manual",
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun | None:
        """触发规则对应的清洗管道。"""
        rule = await self.get_rule(rule_id)
        if rule is None or not rule.enabled:
            return None

        return await self._create_run_and_task(
            run_id=uuid4(),
            rule_id=rule_id,
            trigger=trigger,
            task_type=rule.task_type,
            handler_version=rule.handler_version,
            config=rule.config,
            dispatcher=dispatcher,
        )

    async def trigger_pipeline_once(
        self,
        task_type: str,
        *,
        config: dict[str, object] | None = None,
        trigger: str = "api",
        dispatcher: TaskDispatcher | None = None,
    ) -> DataPipelineRun:
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
