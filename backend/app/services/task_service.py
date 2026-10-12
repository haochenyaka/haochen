"""后台任务用例：提交（去重）、执行（抢占 → 心跳 → 执行体 → 终态）、恢复（补发与收敛）。

不变量：
- 提交只落库 + 返回投递意图，投递由调用方在提交之后进行，失败不阻断业务；
- 去重键命中活动任务（pending/running）时复用既有任务，不产生第二次副作用；
- 执行只接受当前 owner token 写终态；心跳失效即失败关闭，不写终态、不伪装成功；
- 未知 handler_version 立即 failed（fail-closed），不重试、不回退版本；
- 失联（租约过期）统一收敛为 failed(worker_lease_expired)，不自动重放未知外部副作用；
- 处理器声明的失败钩子与终态写入在同一事务内提交（关联对象不悬空）。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.domain import TASK_MAX_CLAIMS
from app.models.task import Task
from app.repositories.task_repository import (
    TASK_LEASE_EXPIRED_ERROR,
    UNKNOWN_HANDLER_ERROR,
    TaskRepository,
)
from app.services.task_dispatch import TaskDispatcher, dispatch_task
from app.services.task_registry import HandlerSpec, TaskContext, resolve_handler

logger = logging.getLogger(__name__)

TASK_LEASE_SECONDS = 300
TASK_HEARTBEAT_INTERVAL_SECONDS = 60
RECOVERY_BATCH_SIZE = 200


@dataclass(frozen=True)
class TaskSubmission:
    """提交结果：`created=False` 表示命中去重键、复用既有活动任务。"""

    task_id: UUID
    task_type: str
    handler_version: int
    status: str
    deduplicated: bool

    @property
    def should_dispatch(self) -> bool:
        """只有新建的 pending 任务需要投递；复用的既有任务已在队列中。"""
        return self.status == "pending" and not self.deduplicated


class TaskService:
    """提交侧用例：由调用方持有事务，提交后自行投递。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tasks = TaskRepository(session)

    async def submit(
        self,
        *,
        task_type: str,
        handler_version: int,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> TaskSubmission:
        if dedupe_key is not None:
            existing = await self._tasks.get_active_by_dedupe_key(dedupe_key)
            if existing is not None:
                return _submission(existing, deduplicated=True)

        try:
            async with self._session.begin_nested():
                task = await self._tasks.create(
                    task_type=task_type,
                    handler_version=handler_version,
                    payload=payload,
                    dedupe_key=dedupe_key,
                )
        except IntegrityError:
            # 并发提交同一去重键：唯一索引裁决，落败方复用胜出行
            if dedupe_key is None:
                raise
            existing = await self._tasks.get_active_by_dedupe_key(dedupe_key)
            if existing is None:
                raise
            return _submission(existing, deduplicated=True)

        return _submission(task, deduplicated=False)


def _submission(task: Task, *, deduplicated: bool) -> TaskSubmission:
    return TaskSubmission(
        task_id=task.id,
        task_type=task.task_type,
        handler_version=task.handler_version,
        status=task.status,
        deduplicated=deduplicated,
    )


async def submit_task(
    *,
    task_type: str,
    handler_version: int,
    payload: dict[str, object],
    dedupe_key: str | None = None,
    dispatcher: TaskDispatcher | None = None,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> TaskSubmission:
    """自持事务的提交入口：落库提交后投递；投递失败保留 pending 由恢复补发。"""
    async with session_factory() as session:
        submission = await TaskService(session).submit(
            task_type=task_type,
            handler_version=handler_version,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        await session.commit()
    if submission.should_dispatch:
        await dispatch_task(dispatcher, submission.task_id)
    return submission


async def cancel_task(
    task_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> bool:
    """取消仅对尚未被抢占的 pending 任务生效；运行中任务由处理器自行复核。"""
    async with session_factory() as session:
        cancelled = await TaskRepository(session).cancel_pending(task_id)
        await session.commit()
    return cancelled


class TaskExecutor:
    def __init__(
        self,
        *,
        dispatcher: TaskDispatcher | None = None,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        lease_seconds: int = TASK_LEASE_SECONDS,
        heartbeat_interval_seconds: float = TASK_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._dispatcher = dispatcher
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    async def execute(self, task_id: UUID) -> str:
        owner_token = uuid4().hex
        async with self._session_factory() as session:
            tasks = TaskRepository(session)
            attempt_no = await tasks.claim(
                task_id, owner_token=owner_token, lease_seconds=self._lease_seconds
            )
            if attempt_no is None:
                exhausted = await tasks.fail_claim_exhausted(task_id)
                await session.commit()
                if exhausted:
                    logger.error("task claim limit exceeded: task_id=%s", task_id)
                    return "claim_limit_exceeded"
                logger.info("task not claimable (running or terminal): task_id=%s", task_id)
                return "not_claimable"

            task = await tasks.get_by_id(task_id)
            await session.commit()

        if task is None:
            return "not_claimable"

        ctx = TaskContext(
            task_id=task_id,
            task_type=task.task_type,
            handler_version=task.handler_version,
            payload=dict(task.payload),
            attempt_no=attempt_no,
            owner_token=owner_token,
            session_factory=self._session_factory,
            dispatcher=self._dispatcher,
        )
        spec = resolve_handler(task.task_type, task.handler_version)
        if spec is None:
            logger.error(
                "unknown task handler, failing closed: task_type=%s handler_version=%s task_id=%s",
                task.task_type,
                task.handler_version,
                task_id,
            )
            return await self._fail(ctx, spec=None, error=UNKNOWN_HANDLER_ERROR, error_class=None)

        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop(ctx, lease_lost=lease_lost))
        try:
            try:
                result = await spec.run(ctx)
            except Exception as exc:
                error_class = type(exc).__name__
                logger.exception("task body failed: task_id=%s type=%s", task_id, task.task_type)
                return await self._fail(
                    ctx, spec=spec, error=str(exc) or error_class, error_class=error_class
                )

            if lease_lost.is_set():
                logger.warning(
                    "task finished after lease loss; terminal write skipped: %s", task_id
                )
                return "lease_lost"
            return await self._succeed(ctx, result=dict(result) if result is not None else None)
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

    async def _heartbeat_loop(self, ctx: TaskContext, *, lease_lost: asyncio.Event) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_interval_seconds)
            try:
                async with self._session_factory() as session:
                    alive = await TaskRepository(session).heartbeat(
                        ctx.task_id, owner_token=ctx.owner_token, lease_seconds=self._lease_seconds
                    )
                    await session.commit()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 数据库瞬时不可用：继续尝试续租；真过期时 reconciler 收敛，
                # 终态写入守卫会拒绝我们，失败关闭由数据兜底。
                logger.warning("task heartbeat error: task_id=%s", ctx.task_id, exc_info=True)
                continue

            if alive:
                continue
            logger.warning("task lease lost: task_id=%s", ctx.task_id)
            lease_lost.set()
            return

    async def _succeed(self, ctx: TaskContext, *, result: dict[str, object] | None) -> str:
        async with self._session_factory() as session:
            written = await TaskRepository(session).mark_succeeded(
                ctx.task_id, owner_token=ctx.owner_token, result=result
            )
            if not written:
                await session.rollback()
                logger.warning("task terminal write rejected: task_id=%s", ctx.task_id)
                return "lease_lost"
            await session.commit()
        return "succeeded"

    async def _fail(
        self,
        ctx: TaskContext,
        *,
        spec: HandlerSpec | None,
        error: str,
        error_class: str | None,
    ) -> str:
        async with self._session_factory() as session:
            written = await TaskRepository(session).mark_failed(
                ctx.task_id, owner_token=ctx.owner_token, error=error
            )
            if not written:
                await session.rollback()
                logger.warning("task terminal write rejected: task_id=%s", ctx.task_id)
                return "lease_lost"
            await _run_failure_hook(
                session, spec=spec, ctx=ctx, error=error, error_class=error_class
            )
            await session.commit()
        return "failed"


async def _run_failure_hook(
    session: AsyncSession,
    *,
    spec: HandlerSpec | None,
    ctx: TaskContext,
    error: str,
    error_class: str | None,
) -> None:
    """失败钩子与终态写入同事务；钩子自身异常只记录，不回滚已成立的失败事实。"""
    if spec is None or spec.on_failure is None:
        return
    try:
        await spec.on_failure(session, ctx, error)
    except Exception:
        logger.exception(
            "task failure hook raised: task_id=%s error_class=%s", ctx.task_id, error_class
        )


async def repost_pending_tasks(
    dispatcher: TaskDispatcher | None,
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    batch_size: int = RECOVERY_BATCH_SIZE,
) -> int:
    """补发 pending 任务（提交后的投递意图，不依赖 Redis 到达保证）。"""
    async with session_factory() as session:
        task_ids = [
            task.id for task in await TaskRepository(session).list_pending(limit=batch_size)
        ]
    for task_id in task_ids:
        await dispatch_task(dispatcher, task_id)
    if task_ids:
        logger.info("reposted pending tasks: count=%d", len(task_ids))
    return len(task_ids)


async def reconcile_expired_tasks(
    *,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
    batch_size: int = RECOVERY_BATCH_SIZE,
) -> int:
    """收敛失联任务：过期租约与 claim 上限耗尽的 pending 行统一置 failed。"""
    async with session_factory() as session:
        tasks = TaskRepository(session)
        expired_ids = await tasks.list_expired_lease_ids(limit=batch_size)
        converged = await tasks.converge_expired(expired_ids)
        exhausted_ids = [
            task_id
            for task_id in await tasks.list_claim_exhausted_ids(limit=batch_size)
            if await tasks.fail_claim_exhausted(task_id)
        ]

        for task_id in converged:
            task = await tasks.get_by_id(task_id)
            if task is None:
                continue
            spec = resolve_handler(task.task_type, task.handler_version)
            ctx = TaskContext(
                task_id=task.id,
                task_type=task.task_type,
                handler_version=task.handler_version,
                payload=dict(task.payload),
                attempt_no=task.attempt_count,
                owner_token="",
                session_factory=session_factory,
            )
            await _run_failure_hook(
                session, spec=spec, ctx=ctx, error=TASK_LEASE_EXPIRED_ERROR, error_class=None
            )
        await session.commit()

    if converged or exhausted_ids:
        logger.warning(
            "reconciled durable tasks: lease_expired=%d claim_exhausted=%d",
            len(converged),
            len(exhausted_ids),
        )
    return len(converged) + len(exhausted_ids)


def context_dispatcher(ctx: dict[str, Any]) -> TaskDispatcher:
    dispatcher = ctx.get("task_dispatcher")
    if dispatcher is None:
        from app.services.task_dispatch import NullTaskDispatcher

        dispatcher = NullTaskDispatcher()
        ctx["task_dispatcher"] = dispatcher
    return cast(TaskDispatcher, dispatcher)


def context_session_factory(ctx: dict[str, Any]) -> async_sessionmaker[AsyncSession]:
    factory = ctx.get("session_factory")
    if factory is None:
        return SessionFactory
    return cast("async_sessionmaker[AsyncSession]", factory)


async def execute_durable_task(ctx: dict[str, Any], task_id: str) -> str:
    """ARQ 作业入口：执行一次后台任务；job 名与 task_dispatch.TASK_JOB_NAME 一致。"""
    executor = TaskExecutor(
        dispatcher=context_dispatcher(ctx),
        session_factory=context_session_factory(ctx),
    )
    return await executor.execute(UUID(task_id))


async def repost_pending_tasks_job(ctx: dict[str, Any]) -> int:
    return await repost_pending_tasks(
        context_dispatcher(ctx), session_factory=context_session_factory(ctx)
    )


async def reconcile_expired_tasks_job(ctx: dict[str, Any]) -> int:
    return await reconcile_expired_tasks(session_factory=context_session_factory(ctx))


__all__ = [
    "TASK_HEARTBEAT_INTERVAL_SECONDS",
    "TASK_LEASE_SECONDS",
    "TASK_MAX_CLAIMS",
    "TaskExecutor",
    "TaskService",
    "TaskSubmission",
    "cancel_task",
    "execute_durable_task",
    "reconcile_expired_tasks",
    "reconcile_expired_tasks_job",
    "repost_pending_tasks",
    "repost_pending_tasks_job",
    "submit_task",
]
