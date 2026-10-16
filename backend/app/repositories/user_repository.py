"""用户仓储：唯一约束兜底并发创建，幂等返回同一身份。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import User
from app.repositories.support import affected_rows


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id, populate_existing=True)

    async def get_by_username(self, username: str) -> User | None:
        stmt = select(User).where(User.username == username)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_or_create(
        self, *, username: str, display_name: str | None = None, role: str = "customer"
    ) -> User:
        existing = await self.get_by_username(username)
        if existing is not None:
            return existing
        user = User(username=username, display_name=display_name, role=role)
        self._session.add(user)
        await self._session.flush()
        return user

    async def create_account(
        self,
        *,
        username: str,
        password_hash: str,
        role: str,
        display_name: str | None = None,
        status: str = "active",
    ) -> User:
        user = User(
            username=username,
            display_name=display_name,
            role=role,
            password_hash=password_hash,
            status=status,
        )
        self._session.add(user)
        await self._session.flush()
        return user

    async def list_all(
        self, *, role: str | None = None, status: str | None = None, search: str | None = None
    ) -> list[User]:
        stmt = select(User)
        if role is not None:
            stmt = stmt.where(User.role == role)
        if status is not None:
            stmt = stmt.where(User.status == status)
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(User.username.ilike(like), User.display_name.ilike(like)))
        stmt = stmt.order_by(User.username)
        return list(
            (await self._session.execute(stmt.execution_options(populate_existing=True))).scalars()
        )

    async def update_fields(self, user_id: UUID, values: dict[str, object]) -> bool:
        result = await self._session.execute(
            update(User)
            .where(User.id == user_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
