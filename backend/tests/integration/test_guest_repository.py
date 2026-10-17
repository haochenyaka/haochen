"""客人档案仓储的并发建档回归：并行工具调用同时首次建档时复用胜出行。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.domain import Guest
from app.repositories import GuestRepository, UserRepository
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


def test_concurrent_get_or_create_reuses_winner(runtime_db_url: str) -> None:
    """两个会话同时首次建档：唯一索引裁决后，落败方重读并复用胜出行。"""

    async def _body(factory: SessionFactoryArg) -> tuple[object, object, int]:
        async with factory() as session:
            user = await UserRepository(session).get_or_create(username=f"guest-race-{uuid4().hex}")
            await session.commit()
            user_id = user.id

        async def winner() -> object:
            async with factory() as session:
                guest = Guest(user_id=user_id)
                session.add(guest)
                await session.flush()
                await asyncio.sleep(0.2)
                await session.commit()
                return guest.id

        async def loser() -> object:
            # 等争胜方先完成 SELECT 与 INSERT，再撞上唯一索引
            await asyncio.sleep(0.05)
            async with factory() as session:
                guest = await GuestRepository(session).get_or_create_by_user(user_id)
                return guest.id

        winner_id, loser_id = await asyncio.gather(winner(), loser())

        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(Guest).where(Guest.user_id == user_id)
            )
        return winner_id, loser_id, int(count or 0)

    winner_id, loser_id, count = run_with_session_factory(runtime_db_url, _body)

    assert count == 1, "并发建档只应留下一行"
    assert loser_id == winner_id, "落败方应复用胜出行"
