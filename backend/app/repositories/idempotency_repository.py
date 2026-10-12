"""通用幂等记录仓储：(scope, key) 唯一，承载「对象.动作」粒度的去重与结果回放。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import IdempotencyRecord
from app.repositories.support import affected_rows


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, scope: str, key: str) -> IdempotencyRecord | None:
        stmt = (
            select(IdempotencyRecord)
            .where(IdempotencyRecord.scope == scope, IdempotencyRecord.key == key)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def create(self, *, scope: str, key: str) -> IdempotencyRecord:
        record = IdempotencyRecord(scope=scope, key=key)
        self._session.add(record)
        await self._session.flush()
        return record

    async def attach_result(self, record_id: UUID, *, result_type: str, result_ref: UUID) -> bool:
        result = await self._session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id)
            .values(result_type=result_type, result_ref=result_ref)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
