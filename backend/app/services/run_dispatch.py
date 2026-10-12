"""Run 投递：投递只允许发生在数据库提交之后，失败不阻断接入。

投递失败时 Run 保持 pending——pending 是已提交的投递意图，由 worker 启动恢复
与周期 publisher 补发，不依赖 Redis 的到达保证。
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.config import get_settings

logger = logging.getLogger(__name__)

RUN_JOB_NAME = "execute_agent_run"


class RunDispatcher(Protocol):
    async def enqueue_run(self, run_id: UUID) -> None: ...

    async def aclose(self) -> None: ...


class ArqRunDispatcher:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    @classmethod
    async def create(cls) -> ArqRunDispatcher:
        pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        return cls(pool)

    async def enqueue_run(self, run_id: UUID) -> None:
        await self._pool.enqueue_job(RUN_JOB_NAME, str(run_id))

    async def aclose(self) -> None:
        await self._pool.aclose()


class NullRunDispatcher:
    """Redis 不可用时的降级实现：仅记录，Run 交由恢复流程补发。"""

    async def enqueue_run(self, run_id: UUID) -> None:
        logger.warning("run dispatcher unavailable; run stays pending: run_id=%s", run_id)

    async def aclose(self) -> None:
        return None


async def dispatch_run(dispatcher: RunDispatcher | None, run_id: UUID | None) -> None:
    """投递失败不阻断接入：Run 保持 pending，由启动恢复与周期 publisher 补发。"""
    if dispatcher is None or run_id is None:
        return
    try:
        await dispatcher.enqueue_run(run_id)
    except Exception:
        logger.warning("run dispatch failed, run stays pending: run_id=%s", run_id, exc_info=True)
