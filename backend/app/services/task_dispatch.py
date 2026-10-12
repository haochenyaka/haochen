"""后台任务投递：与 Run 队列分离，长知识任务不占 AgentRun 槽位。

投递失败不阻断业务提交：pending 是已提交的执行意图，由 worker 启动恢复与周期
publisher 补发；投递与执行都不依赖 Redis 的到达保证。
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.config import get_settings

logger = logging.getLogger(__name__)

TASK_QUEUE_NAME = "haochen:tasks"
TASK_JOB_NAME = "execute_durable_task"


class TaskDispatcher(Protocol):
    async def enqueue_task(self, task_id: UUID) -> None: ...

    async def aclose(self) -> None: ...


class ArqTaskDispatcher:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    @classmethod
    async def create(cls) -> ArqTaskDispatcher:
        pool = await create_pool(
            RedisSettings.from_dsn(get_settings().redis_url),
            default_queue_name=TASK_QUEUE_NAME,
        )
        return cls(pool)

    async def enqueue_task(self, task_id: UUID) -> None:
        await self._pool.enqueue_job(TASK_JOB_NAME, str(task_id))

    async def aclose(self) -> None:
        await self._pool.aclose()


class NullTaskDispatcher:
    """Redis 不可用时的降级实现：仅记录，任务交由恢复流程补发。"""

    async def enqueue_task(self, task_id: UUID) -> None:
        logger.warning("task dispatcher unavailable; task stays pending: task_id=%s", task_id)

    async def aclose(self) -> None:
        return None


async def dispatch_task(dispatcher: TaskDispatcher | None, task_id: UUID | None) -> None:
    if dispatcher is None or task_id is None:
        return
    try:
        await dispatcher.enqueue_task(task_id)
    except Exception:
        logger.warning(
            "task dispatch failed, task stays pending: task_id=%s", task_id, exc_info=True
        )
