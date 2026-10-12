"""ARQ 入口：Run 执行作业 + 周期恢复任务（启动补发 pending、周期收敛过期租约）。

投递可靠性不依赖 Redis 到达保证：pending 是已提交的投递意图，由启动恢复与周期
publisher 补发；运行中租约过期由 reconciler 收敛为 failed 并接力队头请求。

后台任务（Durable Task）走独立队列与独立 worker 进程：长知识任务的槽位由
`ARQ_MAX_JOBS` 配置，不能占满 AgentRun 容量（机制基线 §2.3）。
"""

import asyncio
import logging
import sys
from typing import Any

from arq import cron, func
from arq.connections import RedisSettings
from arq.logs import default_log_config

from app.agents import close_shared_checkpointer
from app.config import get_settings
from app.knowledge.recovery import release_expired_claims_job
from app.lock.task_handlers import submit_daily_rotation_job
from app.services.run_dispatch import ArqRunDispatcher
from app.services.run_event_bus import create_run_event_bus
from app.services.run_worker import (
    execute_agent_run,
    reconcile_expired_job,
    reconcile_expired_runs,
    repost_pending_job,
    repost_pending_runs,
)
from app.services.task_dispatch import TASK_QUEUE_NAME, ArqTaskDispatcher
from app.services.task_service import (
    execute_durable_task,
    reconcile_expired_tasks,
    reconcile_expired_tasks_job,
    repost_pending_tasks,
    repost_pending_tasks_job,
)

logger = logging.getLogger(__name__)

RUN_JOB_TIMEOUT_SECONDS = 900
TASK_JOB_TIMEOUT_SECONDS = 1800

if sys.platform == "win32":  # 本地 Windows 开发：psycopg 异步驱动不支持默认 Proactor 事件循环
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def _build_log_config() -> dict[str, Any]:
    """arq 默认配置只挂载 arq 日志 handler，这里补上应用日志以便 worker 可观察。"""
    config = default_log_config(verbose=False)
    config["loggers"]["app"] = {
        "handlers": ["arq.standard"],
        "level": "INFO",
        "propagate": False,
    }
    return config


WORKER_LOG_CONFIG: dict[str, Any] = _build_log_config()


async def startup(ctx: dict[str, Any]) -> None:
    dispatcher = ArqRunDispatcher(ctx["redis"])
    ctx["run_dispatcher"] = dispatcher
    ctx["run_event_bus"] = await create_run_event_bus()
    try:
        reposted = await repost_pending_runs(dispatcher)
        converged = await reconcile_expired_runs(dispatcher)
    except Exception:
        logger.warning("startup recovery failed; periodic jobs will retry", exc_info=True)
    else:
        logger.info("worker started: reposted=%d converged=%d", reposted, converged)


async def task_startup(ctx: dict[str, Any]) -> None:
    dispatcher = ArqTaskDispatcher(ctx["redis"])
    ctx["task_dispatcher"] = dispatcher
    try:
        reposted = await repost_pending_tasks(dispatcher)
        converged = await reconcile_expired_tasks()
    except Exception:
        logger.warning("task startup recovery failed; periodic jobs will retry", exc_info=True)
    else:
        logger.info("task worker started: reposted=%d converged=%d", reposted, converged)


async def task_shutdown(ctx: dict[str, Any]) -> None:
    dispatcher = ctx.get("task_dispatcher")
    if dispatcher is not None:
        await dispatcher.aclose()
    logger.info("task worker stopped")


async def shutdown(ctx: dict[str, Any]) -> None:
    bus = ctx.get("run_event_bus")
    if bus is not None:
        await bus.aclose()
    await close_shared_checkpointer()
    logger.info("worker stopped")


async def ping(ctx: dict[str, Any]) -> str:
    return "pong"


class WorkerSettings:
    """Run 执行不自动重试：恢复依赖 pending 补发与租约收敛，重试会放大重复执行。"""

    functions: list[Any] = [
        ping,
        func(execute_agent_run, timeout=RUN_JOB_TIMEOUT_SECONDS, max_tries=1),
    ]
    cron_jobs: list[Any] = [
        cron(repost_pending_job, second={0, 30}),
        cron(reconcile_expired_job, second={10, 40}),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 10
    job_timeout = 300
    health_check_interval = 10


class TaskWorkerSettings:
    """后台任务 worker：独立队列 + 独立槽位，长任务不占 AgentRun 容量。

    执行不自动重试：失联由 reconciler 收敛为 failed，重试语义由领域 Handler 决定
    （未知外部副作用不自动重放）。
    """

    functions: list[Any] = [
        ping,
        func(execute_durable_task, timeout=TASK_JOB_TIMEOUT_SECONDS, max_tries=1),
    ]
    # 与 Run worker 的 cron 错开相位，避免两个 worker 同时扫描
    cron_jobs: list[Any] = [
        cron(repost_pending_tasks_job, second={5, 35}),
        cron(reconcile_expired_tasks_job, second={15, 45}),
        cron(release_expired_claims_job, second={25}),
        # 每日轮换：全天每 5 分钟探测，酒店本地 00:05 起提交当日任务（去重键保证一天一次）
        cron(submit_daily_rotation_job, minute=set(range(0, 60, 5)), second={0}),
    ]
    on_startup = task_startup
    on_shutdown = task_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    queue_name = TASK_QUEUE_NAME
    max_jobs = get_settings().arq_max_jobs
    job_timeout = TASK_JOB_TIMEOUT_SECONDS
    health_check_interval = 10
