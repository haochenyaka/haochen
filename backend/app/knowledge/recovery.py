"""知识库恢复路径：claim 过期的在途文档释放回可重试的 error 状态。

失联 worker 留下的 parsing/indexing 行不会自行解冻；本作业把过期 claim 归位，
由管理员重新触发（不自动重放，避免外部副作用重复执行）。
"""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.repositories import KbDocumentRepository

logger = logging.getLogger(__name__)


async def release_expired_claim_documents(
    *, session_factory: async_sessionmaker[AsyncSession] = SessionFactory
) -> int:
    async with session_factory() as session:
        released = await KbDocumentRepository(session).release_expired_claims()
        await session.commit()
    if released:
        logger.warning("released expired kb document claims: count=%d", released)
    return released


async def release_expired_claims_job(ctx: dict[str, Any]) -> int:
    factory = ctx.get("session_factory")
    if factory is None:
        return await release_expired_claim_documents()
    return await release_expired_claim_documents(
        session_factory=cast("async_sessionmaker[AsyncSession]", factory)
    )


__all__ = ["release_expired_claim_documents", "release_expired_claims_job"]
