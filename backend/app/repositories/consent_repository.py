"""同意记录仓储：追加事件 + 按用途取最新状态。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.privacy import ConsentRecord


class ConsentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        user_id: UUID,
        purpose: str,
        policy_version: str,
        granted: bool,
        channel: str,
    ) -> ConsentRecord:
        record = ConsentRecord(
            user_id=user_id,
            purpose=purpose,
            policy_version=policy_version,
            granted=granted,
            channel=channel,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def latest_for_user(self, user_id: UUID) -> dict[str, ConsentRecord]:
        stmt = (
            select(ConsentRecord)
            .where(ConsentRecord.user_id == user_id)
            .order_by(ConsentRecord.created_at.desc(), ConsentRecord.id.desc())
        )
        latest: dict[str, ConsentRecord] = {}
        for row in (await self._session.execute(stmt)).scalars():
            latest.setdefault(row.purpose, row)
        return latest

    async def latest_for(self, user_id: UUID, purpose: str) -> ConsentRecord | None:
        stmt = (
            select(ConsentRecord)
            .where(ConsentRecord.user_id == user_id, ConsentRecord.purpose == purpose)
            .order_by(ConsentRecord.created_at.desc(), ConsentRecord.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
