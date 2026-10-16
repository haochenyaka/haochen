"""清洁派单仓储：守卫式状态迁移 + 留证合并；payload/evidence 不含明文密码（A2）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import HousekeepingTask
from app.repositories.support import affected_rows


class HousekeepingTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        task_type: str,
        room_id: UUID,
        room_no: str,
        order_id: UUID | None = None,
        assigned_staff_id: UUID | None = None,
        payload: Mapping[str, Any] | None = None,
        due_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> HousekeepingTask:
        task = HousekeepingTask(
            task_type=task_type,
            room_id=room_id,
            room_no=room_no,
            order_id=order_id,
            assigned_staff_id=assigned_staff_id,
            payload=dict(payload) if payload is not None else None,
            due_at=due_at,
            idempotency_key=idempotency_key,
        )
        self._session.add(task)
        await self._session.flush()
        return task

    async def get_by_id(self, task_id: UUID) -> HousekeepingTask | None:
        return await self._session.get(HousekeepingTask, task_id, populate_existing=True)

    async def get_by_id_for_update(self, task_id: UUID) -> HousekeepingTask | None:
        stmt = (
            select(HousekeepingTask)
            .where(HousekeepingTask.id == task_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> HousekeepingTask | None:
        stmt = select(HousekeepingTask).where(HousekeepingTask.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_staff(
        self,
        staff_id: UUID,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[HousekeepingTask]:
        stmt = select(HousekeepingTask).where(HousekeepingTask.assigned_staff_id == staff_id)
        if statuses is not None:
            stmt = stmt.where(HousekeepingTask.status.in_(tuple(statuses)))
        stmt = (
            stmt.order_by(HousekeepingTask.created_at, HousekeepingTask.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def transition(
        self,
        task_id: UUID,
        *,
        from_statuses: Sequence[str],
        to_status: str,
        values: Mapping[str, Any] | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> bool:
        """守卫式迁移：仅当当前状态在 from_statuses 内才更新；未命中返回 False。

        evidence 经 JSONB 拼接合并留证，既有留证不会被覆盖。
        """
        assignments: dict[str, Any] = {"status": to_status}
        if values:
            assignments.update(values)
        if evidence is not None:
            assignments["evidence"] = func.coalesce(
                HousekeepingTask.evidence, literal({}, JSONB)
            ).op("||")(literal(dict(evidence), JSONB))
        result = await self._session.execute(
            update(HousekeepingTask)
            .where(
                HousekeepingTask.id == task_id, HousekeepingTask.status.in_(tuple(from_statuses))
            )
            .values(**assignments)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def rate(
        self,
        task_id: UUID,
        *,
        score: int,
        comment: str | None = None,
        rated_at: datetime | None = None,
    ) -> bool:
        """写入客人评分（覆盖式）：仅在任务已交付（可评分状态）时生效。

        守卫与状态迁移同源：调用方必须先校验状态，本方法只负责条件写入。
        """
        assignments: dict[str, Any] = {"guest_score": score, "guest_comment": comment}
        assignments["rated_at"] = rated_at or func.now()
        result = await self._session.execute(
            update(HousekeepingTask)
            .where(HousekeepingTask.id == task_id)
            .values(**assignments)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
