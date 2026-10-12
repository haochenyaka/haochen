"""支付单仓储：transaction_id 幂等读取与守卫式状态落库（A3）。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Payment
from app.repositories.support import affected_rows


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        order_id: UUID,
        guest_id: UUID,
        openid: str,
        amount_fen: int,
        prepay_id: str | None = None,
        status: str = "created",
    ) -> Payment:
        payment = Payment(
            order_id=order_id,
            guest_id=guest_id,
            openid=openid,
            amount_fen=amount_fen,
            prepay_id=prepay_id,
            status=status,
        )
        self._session.add(payment)
        await self._session.flush()
        return payment

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return await self._session.get(Payment, payment_id, populate_existing=True)

    async def get_by_transaction_id(self, transaction_id: str) -> Payment | None:
        stmt = select(Payment).where(Payment.transaction_id == transaction_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_latest_by_order(self, order_id: UUID) -> Payment | None:
        stmt = (
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def mark_succeeded(self, payment_id: UUID, *, transaction_id: str) -> bool:
        """守卫式成功落库：仅 created 可迁移，重复回调不再产生第二条事实。"""
        result = await self._session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == "created")
            .values(status="success", transaction_id=transaction_id, notified_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def mark_failed(self, payment_id: UUID) -> bool:
        """守卫式失败落库：已成功或已失败的支付单不因乱序回调被改写。"""
        result = await self._session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status == "created")
            .values(status="failed", notified_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1
