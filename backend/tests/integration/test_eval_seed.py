"""评测前置夹具验证：paid 夹具落库为已支付订单，且重复运行释放旧单（保证可复现）。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.states import ORDER_TERMINAL_STATUSES
from app.eval.agent.seeds import SEED_PAID, apply_seed, release_eval_orders
from app.models.domain import Order, Room, RoomType
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

SEED_TEST_USERNAME = "eval-seed-fixture-test"


async def _ensure_inventory(factory: SessionFactoryArg) -> None:
    """临时库无房型/房间种子：先补一个可售房型与一间房，夹具才有落库目标。"""
    async with factory() as session:
        room_type = RoomType(
            code="TWIN", name="测试双床房", base_price_fen=45800, capacity=2, bed_type="twin"
        )
        session.add(room_type)
        await session.flush()
        session.add(Room(room_no="T-401", room_type_id=room_type.id, floor=4))
        await session.commit()


def test_paid_seed_yields_paid_order_and_releases_previous(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> tuple[str, str, bool, int]:
        await _ensure_inventory(factory)
        first = await apply_seed(
            session_factory=factory, username=SEED_TEST_USERNAME, seed=SEED_PAID
        )
        second = await apply_seed(
            session_factory=factory, username=SEED_TEST_USERNAME, seed=SEED_PAID
        )
        async with factory() as session:
            rows = {
                order.order_no: order
                for order in (
                    await session.execute(select(Order).where(Order.order_no.in_([first, second])))
                ).scalars()
            }
            active = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Order)
                        .where(
                            Order.guest_id == rows[second].guest_id,
                            Order.status.not_in(ORDER_TERMINAL_STATUSES),
                        )
                    )
                ).scalar_one()
            )
        return (
            rows[first].status,
            rows[second].status,
            rows[second].paid_at is None,
            active,
        )

    first_status, second_status, paid_at_missing, active = run_with_session_factory(
        runtime_db_url, _body
    )

    assert first_status == "cancelled", "重复运行应释放上一次的夹具订单"
    assert second_status == "paid", "paid 夹具应把订单推进到已支付"
    assert not paid_at_missing, "已支付订单必须写入 paid_at"
    assert active == 1, "夹具客人至多一笔未终态订单（房态占用可复现）"


def test_release_eval_orders_retires_active_orders(runtime_db_url: str) -> None:
    """逐条用例开跑前的释放必须把评测客人的未终态订单推进到终态。"""

    async def _body(factory: SessionFactoryArg) -> tuple[int, str]:
        await _ensure_inventory(factory)
        order_no = await apply_seed(
            session_factory=factory, username=SEED_TEST_USERNAME, seed=SEED_PAID
        )
        released = await release_eval_orders(session_factory=factory)
        async with factory() as session:
            order = (
                await session.execute(select(Order).where(Order.order_no == order_no))
            ).scalar_one()
        return released, order.status

    released, status = run_with_session_factory(runtime_db_url, _body)

    assert released >= 1, "释放扫描应命中未终态的评测订单"
    assert status == "cancelled", "释放后订单应落到终态，房态库存不再被占用"
