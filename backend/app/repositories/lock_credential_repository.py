"""门锁凭证仓储：只存 code_hash 与生效窗口；同房间至多一条 active、至多一条 next。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import LockCredential
from app.repositories.support import affected_rows


class LockCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        room_id: UUID,
        room_no: str,
        code_hash: str,
        reason: str,
        version: int,
        valid_from: datetime,
        valid_to: datetime,
        status: str = "active",
        device_credential_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> LockCredential:
        credential = LockCredential(
            room_id=room_id,
            room_no=room_no,
            code_hash=code_hash,
            reason=reason,
            version=version,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
            device_credential_id=device_credential_id,
            idempotency_key=idempotency_key,
        )
        self._session.add(credential)
        await self._session.flush()
        return credential

    async def get_by_id(self, credential_id: UUID) -> LockCredential | None:
        return await self._session.get(LockCredential, credential_id, populate_existing=True)

    async def get_active_for_room(self, room_no: str) -> LockCredential | None:
        stmt = select(LockCredential).where(
            LockCredential.room_no == room_no, LockCredential.status == "active"
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_next_for_room(self, room_no: str) -> LockCredential | None:
        stmt = select(LockCredential).where(
            LockCredential.room_no == room_no, LockCredential.status == "next"
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, idempotency_key: str) -> LockCredential | None:
        stmt = select(LockCredential).where(LockCredential.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def latest_version(self, room_no: str) -> int:
        stmt = select(func.max(LockCredential.version)).where(LockCredential.room_no == room_no)
        return int((await self._session.execute(stmt)).scalar_one_or_none() or 0)

    async def revoke(
        self, credential_id: UUID, *, from_statuses: Sequence[str] = ("active",)
    ) -> bool:
        """吊销凭证（只改状态与吊销时刻，保留历史事实）；默认只吊销 active 行。"""
        result = await self._session.execute(
            update(LockCredential)
            .where(
                LockCredential.id == credential_id,
                LockCredential.status.in_(tuple(from_statuses)),
            )
            .values(status="revoked", revoked_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def activate_next(self, credential_id: UUID) -> bool:
        """预生成凭证转正（next → active）；仅当仍为 next 状态才生效（守卫式）。"""
        result = await self._session.execute(
            update(LockCredential)
            .where(LockCredential.id == credential_id, LockCredential.status == "next")
            .values(status="active")
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def update_fields(self, credential_id: UUID, values: Mapping[str, Any]) -> bool:
        result = await self._session.execute(
            update(LockCredential)
            .where(LockCredential.id == credential_id)
            .values(**dict(values))
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
