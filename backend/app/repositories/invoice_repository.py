"""发票仓储：一张订单至多一张发票（order_id 幂等键兜底并发）。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.domain import Invoice


class InvoiceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        order_id: UUID,
        guest_id: UUID,
        title: str,
        tax_no: str | None,
        amount_fen: int,
        idempotency_key: str | None = None,
    ) -> Invoice:
        invoice = Invoice(
            order_id=order_id,
            guest_id=guest_id,
            title=title,
            tax_no=tax_no,
            amount_fen=amount_fen,
            idempotency_key=idempotency_key,
        )
        self._session.add(invoice)
        await self._session.flush()
        return invoice

    async def get_by_id(self, invoice_id: UUID) -> Invoice | None:
        return await self._session.get(Invoice, invoice_id, populate_existing=True)

    async def get_by_order(self, order_id: UUID) -> Invoice | None:
        stmt = select(Invoice).where(Invoice.order_id == order_id)
        return (await self._session.execute(stmt)).scalars().first()

    async def get_by_idempotency_key(self, idempotency_key: str) -> Invoice | None:
        stmt = select(Invoice).where(Invoice.idempotency_key == idempotency_key)
        return (await self._session.execute(stmt)).scalar_one_or_none()
