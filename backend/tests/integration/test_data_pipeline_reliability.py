"""数据管道集成测试（真实 PostgreSQL）：运行终态、并发互斥、归档正确性、调度器。

覆盖 PR-D1 的三个真 bug 回归：
  - bug 2：build_aggregates 失败必须写 failed 终态（而非静默 return）。
  - bug 3：archive_audit_logs 用真实主键 id 归档，tool_audit 不再必然失败。
  - bug 4：run 行与 task 行在同一事务提交，run 不再永卡 running。
以及 PR-D3 调度器的命中判定与分钟级去重。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.data_pipeline import DataCleaningRule, DataPipelineRun
from app.services.data_pipeline_service import DataPipelineService
from app.services.task_registry import TaskContext
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


class RecordingTaskDispatcher:
    def __init__(self) -> None:
        self.enqueued: list[UUID] = []

    async def enqueue_task(self, task_id: UUID) -> None:
        self.enqueued.append(task_id)

    async def aclose(self) -> None:
        return None


async def _create_running_run(factory: SessionFactoryArg, task_type: str) -> UUID:
    """直接造一条 running 的 run 行，返回 run_id（供 handler 级终态测试）。"""
    run_id = uuid4()
    async with factory() as session:
        session.add(
            DataPipelineRun(
                id=run_id, rule_id=None, trigger="manual", task_type=task_type, status="running"
            )
        )
        await session.commit()
    return run_id


def _ctx(factory: SessionFactoryArg, run_id: UUID, **payload: Any) -> TaskContext:
    return TaskContext(
        task_id=uuid4(),
        task_type="data_pipeline.x",
        handler_version=1,
        payload={"pipeline_run_id": str(run_id), **payload},
        attempt_no=1,
        owner_token="test",
        session_factory=factory,
    )


def test_build_aggregates_failure_writes_failed_terminal(runtime_db_url: str) -> None:
    """回归 bug 2：预聚合失败必须落 failed 终态，不再静默返回。"""
    from app.data_pipeline import task_handlers
    from app.data_pipeline.lake_storage import LakeStorage

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        run_id = await _create_running_run(factory, "data_pipeline.build_aggregates")

        real = LakeStorage.from_settings.__func__  # type: ignore[attr-defined]

        class _BoomStorage:
            async def list_partitions(self, _table: str) -> list[str]:
                raise RuntimeError("lake unreachable")

        def _fake_from_settings(cls: Any) -> Any:  # noqa: ANN401
            return _BoomStorage()

        original = LakeStorage.from_settings
        LakeStorage.from_settings = classmethod(_fake_from_settings)  # type: ignore[assignment]
        try:
            with pytest.raises(RuntimeError):
                await task_handlers.build_aggregates(_ctx(factory, run_id))
        finally:
            LakeStorage.from_settings = original  # type: ignore[assignment]
            _ = real

        async with factory() as session:
            row = (
                await session.execute(select(DataPipelineRun).where(DataPipelineRun.id == run_id))
            ).scalar_one()
        return {"status": row.status, "error": row.error}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["status"] == "failed", "预聚合失败必须写 failed 终态"
    assert "lake unreachable" in (result["error"] or "")


def test_archive_audit_logs_archives_by_real_primary_key(runtime_db_url: str) -> None:
    """回归 bug 3：tool_audit 归档用真实 id，不再因 run_id 类型不匹配必然失败。"""
    from app.data_pipeline import task_handlers

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        run_id = await _create_running_run(factory, "data_pipeline.archive_audit_logs")
        old = datetime.now(UTC) - timedelta(days=200)
        async with factory() as session:
            await session.execute(
                text("""
                    INSERT INTO tool_audit
                        (id, run_id, seq, agent_slug, tool_name, status, created_at)
                    VALUES (9001, 'run-x', 1, 'front-desk', 'demo_tool', 'completed', :old)
                """),
                {"old": old},
            )
            await session.commit()

        await task_handlers.archive_audit_logs(
            _ctx(factory, run_id, config={"retention_days": 90, "tables": ["tool_audit"]})
        )

        async with factory() as session:
            archived = (
                (
                    await session.execute(
                        text(
                            "SELECT source_table, source_id FROM data_archives "
                            "WHERE source_table='tool_audit'"
                        )
                    )
                )
                .mappings()
                .all()
            )
            soft_deleted = (
                await session.execute(text("SELECT deleted_at FROM tool_audit WHERE id=9001"))
            ).scalar_one()
            run_row = (
                await session.execute(select(DataPipelineRun).where(DataPipelineRun.id == run_id))
            ).scalar_one()
        return {
            "archived": [dict(a) for a in archived],
            "soft_deleted": soft_deleted,
            "run_status": run_row.status,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["archived"] == [{"source_table": "tool_audit", "source_id": 9001}]
    assert result["soft_deleted"] is not None, "主表应被软删除"
    assert result["run_status"] == "completed"


def test_trigger_commits_run_and_task_together(runtime_db_url: str) -> None:
    """回归 bug 4：触发后 run 行与其 task 行在同一事务可见，run 不再永卡 running。"""
    from app.models.task import Task

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            rule = DataCleaningRule(
                name="r1",
                task_type="data_pipeline.clean_conversations",
                handler_version=1,
                config={},
                enabled=True,
            )
            session.add(rule)
            await session.commit()
            rule_id = rule.id

        dispatcher = RecordingTaskDispatcher()
        async with factory() as session:
            svc = DataPipelineService(session)
            run = await svc.trigger_pipeline(rule_id, dispatcher=dispatcher)  # type: ignore[arg-type]
            assert run is not None
            run_id = run.id

        async with factory() as session:
            run_row = (
                await session.execute(select(DataPipelineRun).where(DataPipelineRun.id == run_id))
            ).scalar_one()
            task_row = (
                await session.execute(select(Task).where(Task.id == run_row.task_id))
            ).scalar_one_or_none()
        return {
            "run_status": run_row.status,
            "task_present": task_row is not None,
            "dispatched": len(dispatcher.enqueued),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["run_status"] == "running"
    assert result["task_present"], "task 行应与 run 行同事务落库"
    assert result["dispatched"] == 1, "提交后应投递一次"


def test_trigger_rejects_concurrent_same_rule(runtime_db_url: str) -> None:
    """并发互斥：同规则已有运行中 run 时，第二次触发返回 None，不产生孤儿 run。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            rule = DataCleaningRule(
                name="r2",
                task_type="data_pipeline.clean_conversations",
                handler_version=1,
                config={},
                enabled=True,
            )
            session.add(rule)
            await session.commit()
            rule_id = rule.id

        dispatcher = RecordingTaskDispatcher()
        async with factory() as session:
            svc = DataPipelineService(session)
            first = await svc.trigger_pipeline(rule_id, dispatcher=dispatcher)  # type: ignore[arg-type]
            second = await svc.trigger_pipeline(rule_id, dispatcher=dispatcher)  # type: ignore[arg-type]

        async with factory() as session:
            count = (
                (
                    await session.execute(
                        select(DataPipelineRun).where(DataPipelineRun.rule_id == rule_id)
                    )
                )
                .scalars()
                .all()
            )
        return {"first_ok": first is not None, "second_none": second is None, "runs": len(count)}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["first_ok"] is True
    assert result["second_none"] is True, "运行中的同规则不应重复触发"
    assert result["runs"] == 1


def test_scheduler_submits_due_rule_and_dedupes_within_minute(runtime_db_url: str) -> None:
    """调度器：命中的规则提交一次 scheduled run；同一分钟内重复调用不重复提交。"""
    from app.data_pipeline.scheduler import submit_due_pipelines

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            session.add(
                DataCleaningRule(
                    name="每分钟",
                    task_type="data_pipeline.validate_kb_consistency",
                    handler_version=1,
                    schedule_cron="* * * * *",
                    config={},
                    enabled=True,
                )
            )
            # 未命中（3:00）的规则
            session.add(
                DataCleaningRule(
                    name="每天三点",
                    task_type="data_pipeline.dedup_feedback",
                    handler_version=1,
                    schedule_cron="0 3 * * *",
                    config={},
                    enabled=True,
                )
            )
            await session.commit()

        dispatcher = RecordingTaskDispatcher()
        moment = datetime(2026, 10, 18, 9, 17, 5, tzinfo=UTC)
        first = await submit_due_pipelines(
            now=moment, session_factory=factory, dispatcher=dispatcher
        )
        # 同一分钟（不同秒）再跑一次：应被分钟级去重键拦下
        second = await submit_due_pipelines(
            now=moment.replace(second=40), session_factory=factory, dispatcher=dispatcher
        )

        async with factory() as session:
            runs = (await session.execute(select(DataPipelineRun))).scalars().all()
        return {
            "first_count": len(first),
            "second_count": len(second),
            "triggers": sorted(r.trigger for r in runs),
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["first_count"] == 1, "仅每分钟规则命中"
    assert result["second_count"] == 0, "同一分钟不重复提交"
    assert result["triggers"] == ["scheduled"]
