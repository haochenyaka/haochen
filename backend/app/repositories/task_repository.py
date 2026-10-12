"""后台任务仓储：条件抢占、心跳续租、终态守卫与去重键复用。

不变量：
- 重复投递不能取得运行中任务：抢占只在 `pending` 且未达 claim 上限时命中；
- 终态写入只接受当前 owner token，落库即释放租约；
- 去重键在 (dedupe_key) 部分唯一索引上兜底，冲突时复用既有活动任务；
- 租约过期一律收敛为 failed(worker_lease_expired)，不自动重放未知外部副作用。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import TASK_MAX_CLAIMS, TASK_TERMINAL_STATUSES
from app.models.task import Task
from app.repositories.support import affected_rows

ACTIVE_TASK_STATUSES: tuple[str, ...] = ("pending", "running")
TASK_LEASE_EXPIRED_ERROR = "worker_lease_expired"
UNKNOWN_HANDLER_ERROR = "unknown_handler_version"


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_type: str,
        handler_version: int,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> Task:
        task = Task(
            task_type=task_type,
            handler_version=handler_version,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get_by_id(self, task_id: UUID) -> Task | None:
        return await self._session.get(Task, task_id, populate_existing=True)

    async def get_active_by_dedupe_key(self, dedupe_key: str) -> Task | None:
        stmt = (
            select(Task)
            .where(Task.dedupe_key == dedupe_key, Task.status.in_(ACTIVE_TASK_STATUSES))
            .order_by(Task.created_at, Task.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def exists_by_dedupe_key(self, dedupe_key: str) -> bool:
        """去重键是否已有任意状态的任务行：批任务「每天至多一次」的提交判定。"""
        stmt = select(Task.id).where(Task.dedupe_key == dedupe_key).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none() is not None

    async def list_pending(self, *, limit: int = 100) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.status == "pending")
            .order_by(Task.created_at, Task.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_by_status(self, status: str, *, limit: int = 100) -> list[Task]:
        stmt = (
            select(Task)
            .where(Task.status == status)
            .order_by(Task.created_at, Task.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def claim(self, task_id: UUID, *, owner_token: str, lease_seconds: int) -> int | None:
        """pending → running；返回本次 attempt 序号；未抢到返回 None。

        守卫条件同时覆盖「重复投递」（已在 running）与 claim 上限（attempt_count 达 4）。
        """
        now = func.now()
        stmt = (
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == "pending",
                Task.attempt_count < TASK_MAX_CLAIMS,
            )
            .values(
                status="running",
                owner_token=owner_token,
                attempt_count=Task.attempt_count + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                last_heartbeat_at=now,
                updated_at=now,
            )
            .returning(Task.attempt_count)
            .execution_options(synchronize_session=False)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        return int(row.attempt_count) if row is not None else None

    async def heartbeat(self, task_id: UUID, *, owner_token: str, lease_seconds: int) -> bool:
        """续租；租约已被接管或任务已终结返回 False（调用方必须失败关闭）。"""
        now = func.now()
        result = await self._session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.owner_token == owner_token,
                Task.status == "running",
            )
            .values(
                last_heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def set_progress(
        self, task_id: UUID, *, owner_token: str, progress: dict[str, object]
    ) -> bool:
        result = await self._session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.owner_token == owner_token,
                Task.status == "running",
            )
            .values(progress=progress, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def mark_succeeded(
        self, task_id: UUID, *, owner_token: str, result: dict[str, object] | None = None
    ) -> bool:
        return await self._write_terminal(
            task_id,
            owner_token=owner_token,
            status="succeeded",
            values={"result": result} if result is not None else {},
        )

    async def mark_failed(self, task_id: UUID, *, owner_token: str, error: str) -> bool:
        return await self._write_terminal(
            task_id, owner_token=owner_token, status="failed", values={"error": error[:2000]}
        )

    async def mark_cancelled(self, task_id: UUID, *, owner_token: str) -> bool:
        return await self._write_terminal(
            task_id, owner_token=owner_token, status="cancelled", values={}
        )

    async def cancel_pending(self, task_id: UUID) -> bool:
        """派发前取消：pending 直接终结（尚无 worker 持有租约）。"""
        result = await self._session.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == "pending")
            .values(
                status="cancelled",
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def fail_claim_exhausted(self, task_id: UUID) -> bool:
        """claim 上限耗尽：无人持有租约的 pending 行置 failed（失败关闭，不静默滞留）。"""
        result = await self._session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.status == "pending",
                Task.attempt_count >= TASK_MAX_CLAIMS,
            )
            .values(
                status="failed",
                error="claim_limit_exceeded",
                finished_at=func.now(),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def list_claim_exhausted_ids(self, *, limit: int = 50) -> list[UUID]:
        stmt = (
            select(Task.id)
            .where(Task.status == "pending", Task.attempt_count >= TASK_MAX_CLAIMS)
            .order_by(Task.created_at, Task.id)
            .limit(limit)
        )
        return [row.id for row in (await self._session.execute(stmt)).all()]

    async def list_expired_lease_ids(self, *, limit: int = 50) -> list[UUID]:
        stmt = (
            select(Task.id)
            .where(
                Task.status == "running",
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at < func.now(),
            )
            .order_by(Task.lease_expires_at)
            .limit(limit)
        )
        return [row.id for row in (await self._session.execute(stmt)).all()]

    async def converge_expired(self, task_ids: Sequence[UUID]) -> list[UUID]:
        """带守卫地收敛过期租约为 failed(worker_lease_expired)；返回真正收敛的 id。"""
        if not task_ids:
            return []
        now = func.now()
        result = await self._session.execute(
            update(Task)
            .where(
                Task.id.in_(tuple(task_ids)),
                Task.status == "running",
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at < now,
            )
            .values(
                status="failed",
                error=TASK_LEASE_EXPIRED_ERROR,
                owner_token=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
            .returning(Task.id)
            .execution_options(synchronize_session=False)
        )
        return [row.id for row in result.all()]

    async def _write_terminal(
        self,
        task_id: UUID,
        *,
        owner_token: str,
        status: str,
        values: dict[str, object],
    ) -> bool:
        if status not in TASK_TERMINAL_STATUSES:
            raise ValueError(f"非终态状态不可作为终态写入：{status}")
        now = func.now()
        payload: dict[str, object] = {
            "status": status,
            "owner_token": None,
            "lease_expires_at": None,
            "finished_at": now,
            "updated_at": now,
        }
        payload.update(values)
        result = await self._session.execute(
            update(Task)
            .where(
                Task.id == task_id,
                Task.owner_token == owner_token,
                Task.status == "running",
            )
            .values(**payload)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
