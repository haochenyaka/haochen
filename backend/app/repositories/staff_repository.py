"""员工档案仓储：工号唯一，user_id 链接接入身份（P5 员工登录后启用）。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Staff
from app.repositories.support import affected_rows


class StaffRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        staff_no: str,
        name: str,
        position: str = "housekeeping",
        user_id: UUID | None = None,
        status: str = "active",
    ) -> Staff:
        staff = Staff(
            staff_no=staff_no, name=name, position=position, user_id=user_id, status=status
        )
        self._session.add(staff)
        await self._session.flush()
        return staff

    async def get_by_id(self, staff_id: UUID) -> Staff | None:
        return await self._session.get(Staff, staff_id, populate_existing=True)

    async def get_by_staff_no(self, staff_no: str) -> Staff | None:
        stmt = select(Staff).where(Staff.staff_no == staff_no)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_user_id(self, user_id: UUID) -> Staff | None:
        stmt = select(Staff).where(Staff.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_all(self, *, status: str | None = None, search: str | None = None) -> list[Staff]:
        stmt = select(Staff)
        if status is not None:
            stmt = stmt.where(Staff.status == status)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(Staff.name.ilike(like), Staff.staff_no.ilike(like)))
        stmt = stmt.order_by(Staff.staff_no)
        return list((await self._session.execute(stmt)).scalars())

    async def update_fields(self, staff_id: UUID, values: dict[str, object]) -> bool:
        result = await self._session.execute(
            update(Staff)
            .where(Staff.id == staff_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
