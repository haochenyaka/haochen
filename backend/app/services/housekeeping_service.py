"""清洁派单状态机服务：行锁串行化迁移 + 留证与时间戳副作用。

不变量：
- `checkout_clean` 进入 verified 前必须确认密码已修改（A1），留证不足一律拒绝；
- payload 与 evidence 不得包含明文密码字段（A2），写入前在领域层拒绝；
- 非法迁移与并发改写抛出 ConflictError，数据库状态保持不变；
- 创建带 idempotency_key 时重复提交返回既有任务，不重复派单（列内唯一约束兜底并发）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from app.domain.states import (
    HOUSEKEEPING_TASK_TYPES,
    HOUSEKEEPING_TIMESTAMP_COLUMNS,
    IllegalTransitionError,
    VerificationRequiredError,
    ensure_housekeeping_transition,
    ensure_verification_evidence,
)
from app.models.domain import HousekeepingTask
from app.repositories.housekeeping_repository import HousekeepingTaskRepository

FORBIDDEN_PLAINTEXT_KEYS: frozenset[str] = frozenset(
    {"password", "new_password", "old_password", "current_password", "plain_password"}
)


def _ensure_no_plaintext_keys(container: Mapping[str, Any] | None, *, where: str) -> None:
    if not container:
        return
    for key in container:
        if str(key).strip().lower() in FORBIDDEN_PLAINTEXT_KEYS:
            raise ValidationFailedError(f"{where}不得包含明文密码字段：{key}")


class HousekeepingService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._tasks = HousekeepingTaskRepository(session)

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
        if task_type not in HOUSEKEEPING_TASK_TYPES:
            raise ValidationFailedError(f"未知清洁任务类型：{task_type}")
        _ensure_no_plaintext_keys(payload, where="任务载荷")
        if idempotency_key is not None:
            replayed = await self._tasks.get_by_idempotency_key(idempotency_key)
            if replayed is not None:
                return replayed
        return await self._tasks.create(
            task_type=task_type,
            room_id=room_id,
            room_no=room_no,
            order_id=order_id,
            assigned_staff_id=assigned_staff_id,
            payload=payload,
            due_at=due_at,
            idempotency_key=idempotency_key,
        )

    async def transition(
        self,
        task_id: UUID,
        to_status: str,
        *,
        assigned_staff_id: UUID | None = None,
        evidence: Mapping[str, Any] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> HousekeepingTask:
        task = await self._tasks.get_by_id_for_update(task_id)
        if task is None:
            raise NotFoundError("housekeeping_task", task_id)
        try:
            ensure_housekeeping_transition(task.status, to_status)
        except IllegalTransitionError as exc:
            raise ConflictError(str(exc)) from exc

        if evidence is not None:
            _ensure_no_plaintext_keys(evidence, where="留证")
        if to_status == "verified":
            merged_evidence = {**(task.evidence or {}), **dict(evidence or {})}
            try:
                ensure_verification_evidence(task.task_type, merged_evidence)
            except VerificationRequiredError as exc:
                raise ConflictError(str(exc)) from exc

        values: dict[str, Any] = dict(extra or {})
        timestamp_column = HOUSEKEEPING_TIMESTAMP_COLUMNS.get(to_status)
        if timestamp_column is not None:
            values[timestamp_column] = func.now()
        if assigned_staff_id is not None:
            values["assigned_staff_id"] = assigned_staff_id

        updated = await self._tasks.transition(
            task.id,
            from_statuses=(task.status,),
            to_status=to_status,
            values=values,
            evidence=evidence,
        )
        if not updated:
            raise ConflictError(f"任务状态并发改写，迁移未生效：{to_status}")
        await self._session.refresh(task)
        return task
