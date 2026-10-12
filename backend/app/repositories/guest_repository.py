"""客人档案仓储：接入身份（users）与客人档案（guests）的链接读取。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Guest
from app.repositories.support import affected_rows


class GuestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: UUID | None = None,
        openid: str | None = None,
        nickname: str | None = None,
        phone: str | None = None,
    ) -> Guest:
        guest = Guest(user_id=user_id, openid=openid, nickname=nickname, phone=phone)
        self._session.add(guest)
        await self._session.flush()
        return guest

    async def get_by_id(self, guest_id: UUID) -> Guest | None:
        return await self._session.get(Guest, guest_id, populate_existing=True)

    async def get_by_user_id(self, user_id: UUID) -> Guest | None:
        stmt = select(Guest).where(Guest.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_openid(self, openid: str) -> Guest | None:
        stmt = select(Guest).where(Guest.openid == openid)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_or_create_by_user(self, user_id: UUID, *, nickname: str | None = None) -> Guest:
        existing = await self.get_by_user_id(user_id)
        if existing is not None:
            return existing
        return await self.create(user_id=user_id, nickname=nickname)

    async def update_fields(self, guest_id: UUID, values: dict[str, object]) -> bool:
        result = await self._session.execute(
            update(Guest)
            .where(Guest.id == guest_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
