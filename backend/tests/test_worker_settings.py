"""Worker 注册契约：ARQ 作业名与投递端一致，恢复任务与启动钩子已注册。"""

from __future__ import annotations

from typing import Any

from app.data_pipeline.scheduler import submit_pipeline_schedules_job
from app.knowledge.recovery import release_expired_claims_job
from app.lock.task_handlers import submit_daily_rotation_job
from app.quality.task_handlers import submit_qa_audit_job
from app.services.run_dispatch import RUN_JOB_NAME
from app.services.run_worker import (
    execute_agent_run,
    reconcile_expired_job,
    repost_pending_job,
)
from app.services.task_dispatch import TASK_JOB_NAME, TASK_QUEUE_NAME
from app.services.task_service import (
    execute_durable_task,
    reconcile_expired_tasks_job,
    repost_pending_tasks_job,
)
from app.worker.settings import (
    RUN_JOB_TIMEOUT_SECONDS,
    TASK_JOB_TIMEOUT_SECONDS,
    WORKER_LOG_CONFIG,
    TaskWorkerSettings,
    WorkerSettings,
    ping,
)


def _function(settings: type, name: str) -> Any:
    for entry in settings.functions:
        entry_name = getattr(entry, "name", None) or getattr(entry, "__name__", None)
        if entry_name == name:
            return entry
    raise AssertionError(f"ARQ 未注册作业：{name}")


def test_run_job_name_matches_dispatcher_contract() -> None:
    assert execute_agent_run.__name__ == RUN_JOB_NAME
    entry = _function(WorkerSettings, RUN_JOB_NAME)
    assert entry.coroutine is execute_agent_run
    assert entry.max_tries == 1
    assert entry.timeout_s == RUN_JOB_TIMEOUT_SECONDS


def test_health_probe_still_registered() -> None:
    assert _function(WorkerSettings, "ping") is ping


def test_cron_recovery_jobs_registered() -> None:
    jobs = {job.name: job for job in WorkerSettings.cron_jobs}
    assert set(jobs) == {"cron:repost_pending_job", "cron:reconcile_expired_job"}
    assert jobs["cron:repost_pending_job"].coroutine is repost_pending_job
    assert jobs["cron:reconcile_expired_job"].coroutine is reconcile_expired_job
    assert jobs["cron:repost_pending_job"].second == {0, 30}
    assert jobs["cron:reconcile_expired_job"].second == {10, 40}


def test_startup_and_shutdown_hooks_registered() -> None:
    assert WorkerSettings.on_startup.__name__ == "startup"
    assert WorkerSettings.on_shutdown.__name__ == "shutdown"


def test_worker_log_config_covers_app_records() -> None:
    app_logger = WORKER_LOG_CONFIG["loggers"]["app"]
    assert app_logger["level"] == "INFO"
    assert app_logger["handlers"] == ["arq.standard"]
    assert WORKER_LOG_CONFIG["handlers"]["arq.standard"]["class"] == "logging.StreamHandler"


def test_task_job_name_matches_dispatcher_contract() -> None:
    assert execute_durable_task.__name__ == TASK_JOB_NAME
    entry = _function(TaskWorkerSettings, TASK_JOB_NAME)
    assert entry.coroutine is execute_durable_task
    assert entry.max_tries == 1
    assert entry.timeout_s == TASK_JOB_TIMEOUT_SECONDS


def test_task_worker_uses_dedicated_queue_and_slots() -> None:
    from app.config import get_settings

    assert TaskWorkerSettings.queue_name == TASK_QUEUE_NAME
    assert TaskWorkerSettings.queue_name != "arq:queue"
    assert TaskWorkerSettings.max_jobs == get_settings().arq_max_jobs
    assert TaskWorkerSettings.max_jobs > 0


def test_task_cron_recovery_jobs_registered() -> None:
    jobs = {job.name: job for job in TaskWorkerSettings.cron_jobs}
    assert set(jobs) == {
        "cron:repost_pending_tasks_job",
        "cron:reconcile_expired_tasks_job",
        "cron:release_expired_claims_job",
        "cron:submit_daily_rotation_job",
        "cron:submit_qa_audit_job",
        "cron:submit_pipeline_schedules_job",
    }
    assert jobs["cron:repost_pending_tasks_job"].coroutine is repost_pending_tasks_job
    assert jobs["cron:reconcile_expired_tasks_job"].coroutine is reconcile_expired_tasks_job
    assert jobs["cron:release_expired_claims_job"].coroutine is release_expired_claims_job
    # 与 Run worker 的相位错开
    assert jobs["cron:repost_pending_tasks_job"].second == {5, 35}
    assert jobs["cron:reconcile_expired_tasks_job"].second == {15, 45}
    assert jobs["cron:release_expired_claims_job"].second == {25}
    # 每日轮换：全天探测（判定与提交在任务内做，见 submit_daily_rotation_job）
    rotation = jobs["cron:submit_daily_rotation_job"]
    assert rotation.coroutine is submit_daily_rotation_job
    assert rotation.minute == set(range(0, 60, 5))
    assert rotation.second == {0}
    # 质检跑批：每小时一次（小时级去重键，重启补发不堆积）
    audit = jobs["cron:submit_qa_audit_job"]
    assert audit.coroutine is submit_qa_audit_job
    assert audit.minute == {10}
    assert audit.second == {0}
    # 数据管道调度：每分钟扫描命中的清洗规则（分钟级去重键）
    pipeline = jobs["cron:submit_pipeline_schedules_job"]
    assert pipeline.coroutine is submit_pipeline_schedules_job
    assert pipeline.minute == set(range(0, 60))
    assert pipeline.second == {20}


def test_task_worker_startup_hooks_registered() -> None:
    assert TaskWorkerSettings.on_startup.__name__ == "task_startup"
    assert TaskWorkerSettings.on_shutdown.__name__ == "task_shutdown"
