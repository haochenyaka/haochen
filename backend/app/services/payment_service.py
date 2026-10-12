"""支付用例服务：发起支付（JSAPI 下单）与回调记账（先落事实、后投递后台任务）。

不变量：
- 发起支付复验订单归属与状态：仅 created / pending_payment 可发起，金额取订单应收事实；
- 回调以 transaction_id 为幂等键：命中既有事实即回放，不产生第二单（A3）；
- 回调金额与订单应收金额不一致：拒绝记账、不写任何事实（告警由调用方记录）；
- 乱序回调不回退已 paid 的订单；失败回调只改支付单、不动订单状态；
- 成功事实只在 PostgreSQL 提交后（调用方 commit）才允许投递 worker；
- 未发起过支付的订单收到回调：以回调为事实补建支付单，不静默丢弃资金流水。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models.domain import Order
from app.repositories.guest_repository import GuestRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.payment_repository import PaymentRepository
from app.services.order_service import OrderService
from app.wechat import PrepayOrder, WechatClient

logger = logging.getLogger(__name__)

PAYABLE_STATUSES: frozenset[str] = frozenset({"created", "pending_payment"})
PAID_STATUSES: frozenset[str] = frozenset({"paid", "checked_in", "checkout_pending", "completed"})


class PaymentAmountMismatchError(ValidationFailedError):
    """回调金额与订单应收金额不一致：拒绝记账并告警。"""


@dataclass(frozen=True)
class PaymentCallbackBody:
    event: str
    order_no: str
    transaction_id: str
    amount_fen: int
    openid: str


@dataclass(frozen=True)
class PaymentInitiation:
    order_no: str
    amount_fen: int
    prepay_id: str
    pay_params: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class PaymentCallbackResult:
    """回调处理结果：`status` ∈ applied/replayed/failed/already_paid/anomaly/unknown_order。

    仅 `applied`（本次真实把订单推进到 paid）需要投递入住准备任务；
    `replayed` 与 `already_paid` 命中既有事实，重复回调不产生第二单。
    """

    status: str
    order_id: UUID | None = None
    payment_id: UUID | None = None
    order_no: str | None = None

    @property
    def checkin_required(self) -> bool:
        return self.status == "applied" and self.order_id is not None


class PaymentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrderRepository(session)
        self._payments = PaymentRepository(session)
        self._guests = GuestRepository(session)

    async def initiate(
        self, *, user_id: UUID, order_no: str, client: WechatClient
    ) -> PaymentInitiation:
        """发起支付：下单成功后订单进入 pending_payment，不改变订单金额事实。"""
        order = await self._require_owned(user_id=user_id, order_no=order_no)
        if order.status not in PAYABLE_STATUSES:
            raise ConflictError(f"订单当前状态（{order.status}）不能发起支付")
        guest = await self._guests.get_by_user_id(user_id)
        if guest is None or not guest.openid:
            raise ValidationFailedError("缺少微信身份，请重新登录后再支付")

        prepay: PrepayOrder = await client.create_pay_order(
            order_no=order.order_no, amount_fen=order.amount_fen, openid=guest.openid
        )
        if order.status == "created":
            await OrderService(self._session).transition(order.id, "pending_payment")
        await self._payments.create(
            order_id=order.id,
            guest_id=order.guest_id,
            openid=guest.openid,
            amount_fen=order.amount_fen,
            prepay_id=prepay.prepay_id,
        )
        return PaymentInitiation(
            order_no=order.order_no,
            amount_fen=order.amount_fen,
            prepay_id=prepay.prepay_id,
            pay_params=dict(prepay.pay_params),
        )

    async def apply_callback(self, body: PaymentCallbackBody) -> PaymentCallbackResult:
        replayed = await self._payments.get_by_transaction_id(body.transaction_id)
        if replayed is not None:
            order = await self._orders.get_by_id(replayed.order_id)
            return PaymentCallbackResult(
                status="replayed",
                order_id=replayed.order_id,
                payment_id=replayed.id,
                order_no=order.order_no if order is not None else body.order_no,
            )

        order = await self._orders.get_by_order_no(body.order_no.strip().upper())
        if order is None:
            logger.warning(
                "支付回调指向未知订单：order_no=%s txn=%s", body.order_no, body.transaction_id
            )
            return PaymentCallbackResult(status="unknown_order", order_no=body.order_no)

        payment = await self._payments.get_latest_by_order(order.id)
        if payment is None:
            payment = await self._payments.create(
                order_id=order.id,
                guest_id=order.guest_id,
                openid=body.openid,
                amount_fen=body.amount_fen,
            )

        if body.amount_fen != order.amount_fen:
            logger.warning(
                "支付回调金额不符：order_no=%s expected=%d got=%d txn=%s",
                order.order_no,
                order.amount_fen,
                body.amount_fen,
                body.transaction_id,
            )
            raise PaymentAmountMismatchError(f"回调金额与订单应收金额不一致：{order.order_no}")

        if body.event == "fail":
            return await self._apply_failure(order, payment.id)

        return await self._apply_success(order, payment.id, body)

    async def _apply_failure(self, order: Order, payment_id: UUID) -> PaymentCallbackResult:
        if order.status in PAID_STATUSES:
            return PaymentCallbackResult(
                status="already_paid",
                order_id=order.id,
                payment_id=payment_id,
                order_no=order.order_no,
            )
        changed = await self._payments.mark_failed(payment_id)
        return PaymentCallbackResult(
            status="failed" if changed else "already_paid",
            order_id=order.id,
            payment_id=payment_id,
            order_no=order.order_no,
        )

    async def _apply_success(
        self, order: Order, payment_id: UUID, body: PaymentCallbackBody
    ) -> PaymentCallbackResult:
        if order.status == "cancelled":
            await self._payments.mark_succeeded(payment_id, transaction_id=body.transaction_id)
            logger.warning(
                "已取消订单收到成功回调，仅记录支付事实待人工核对：order_no=%s txn=%s",
                order.order_no,
                body.transaction_id,
            )
            return PaymentCallbackResult(
                status="anomaly", order_id=order.id, payment_id=payment_id, order_no=order.order_no
            )

        marked = await self._payments.mark_succeeded(payment_id, transaction_id=body.transaction_id)
        if not marked:
            # 并发重复回调：另一请求已落支付事实，本次按回放处理，不重复迁移订单
            return PaymentCallbackResult(
                status="replayed",
                order_id=order.id,
                payment_id=payment_id,
                order_no=order.order_no,
            )

        if order.status in PAID_STATUSES:
            return PaymentCallbackResult(
                status="already_paid",
                order_id=order.id,
                payment_id=payment_id,
                order_no=order.order_no,
            )

        service = OrderService(self._session)
        if order.status == "created":
            await service.transition(order.id, "pending_payment")
        paid = await service.transition(
            order.id,
            "paid",
            extra={"paid_amount_fen": body.amount_fen},
            event={
                "type": "paid",
                "channel": "wechat",
                "transaction_id": body.transaction_id,
                "amount_fen": body.amount_fen,
            },
        )
        return PaymentCallbackResult(
            status="applied", order_id=paid.id, payment_id=payment_id, order_no=paid.order_no
        )

    async def _require_owned(self, *, user_id: UUID, order_no: str) -> Order:
        normalized = order_no.strip().upper()
        order = await self._orders.get_by_order_no(normalized)
        if order is None:
            raise NotFoundError("order", normalized)
        guest = await self._guests.get_by_user_id(user_id)
        if guest is None or order.guest_id != guest.id:
            raise PermissionDeniedError(f"订单不属于当前客人：{normalized}")
        return order
