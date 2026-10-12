"""员工任务只读用例：按身份链接的员工档案过滤，跨员工访问一律拒绝（A6）。

不变量：
- 可见性由 `assigned_staff_id == 当前员工档案` 决定，路由层的角色判定不替代此处归属校验；
- 账号未绑定员工档案按越权处理（403），不泄露员工目录结构；
- 越权读取不产生任何写副作用。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import NotFoundError, PermissionDeniedError
from app.models.domain import HousekeepingTask, Staff
from app.repositories.housekeeping_repository import HousekeepingTaskRepository
from app.repositories.staff_repository import StaffRepository

DEFAULT_TASK_LIMIT = 50
MAX_TASK_LIMIT = 200


class StaffTaskService:
    def __init__(self, session: AsyncSession) -> None:
        self._staff = StaffRepository(session)
        self._tasks = HousekeepingTaskRepository(session)

    async def resolve_profile(self, user_id: UUID) -> Staff:
        staff = await self._staff.get_by_user_id(user_id)
        if staff is None:
            raise PermissionDeniedError("当前账号未绑定员工档案")
        return staff

    async def list_my_tasks(
        self,
        user_id: UUID,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = DEFAULT_TASK_LIMIT,
    ) -> list[HousekeepingTask]:
        profile = await self.resolve_profile(user_id)
        return await self._tasks.list_for_staff(
            profile.id, statuses=statuses, limit=min(limit, MAX_TASK_LIMIT)
        )

    async def get_my_task(self, user_id: UUID, task_id: UUID) -> HousekeepingTask:
        profile = await self.resolve_profile(user_id)
        task = await self._tasks.get_by_id(task_id)
        if task is None:
            raise NotFoundError("housekeeping_task", task_id)
        if task.assigned_staff_id != profile.id:
            raise PermissionDeniedError("该任务未指派给当前员工")
        return task
