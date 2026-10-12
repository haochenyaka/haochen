"""订单工具：房型查询、我的订单、预订、改期、续订、取消与发票申请。

写类工具执行处复验：身份派生客人档案，订单归属、房型有效、日期窗与房态库存由服务层
复核，失败关闭；每次调用带稳定幂等键（Run + 工具调用 ID），worker 恢复重放不产生第二次
副作用——订单走列内唯一键，其余动作走通用幂等组件（并发由唯一约束兜底）。业务失败转为
模型可见的中文文本并留日志，Run 不因单次业务失败中断。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from uuid import UUID

from langgraph.prebuilt import ToolRuntime
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.runtime import RuntimeContext
from app.agents.toolkits.registry import builtin_tool
from app.domain.errors import (
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models.domain import Order
from app.repositories import (
    GuestRepository,
    InvoiceRepository,
    OrderRepository,
    RoomTypeRepository,
)
from app.services.booking_service import renew_booking_with_credential
from app.services.idempotency_service import IdempotencyService
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)

ORDER_STATUS_LABELS: dict[str, str] = {
    "created": "已创建（待发起支付）",
    "pending_payment": "待支付",
    "paid": "已支付",
    "checked_in": "已入住",
    "checkout_pending": "退房处理中",
    "completed": "已完成",
    "cancelled": "已取消",
}
INVOICE_STATUS_LABELS: dict[str, str] = {"requested": "开票中", "issued": "已开具"}
ORDER_NOT_FOUND_TEXT = "没有找到对应的订单，请核对订单号后重试。"
RECENT_ORDER_LIMIT = 5

Operation = Callable[[AsyncSession, UUID], Awaitable[tuple[str, UUID]]]


def _failure(reason: str) -> str:
    logger.warning("订单工具失败：%s", reason)
    return f"操作失败：{reason}"


def _domain_failure(exc: DomainError) -> str:
    """业务失败 → 模型可见文本：归属/不存在统一措辞，不泄露订单是否存在。"""
    logger.warning("订单工具失败：%s: %s", type(exc).__name__, exc)
    if isinstance(exc, (NotFoundError, PermissionDeniedError)):
        return ORDER_NOT_FOUND_TEXT
    return f"操作失败：{exc}"


def _parse_date(raw: str, label: str) -> date:
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValidationFailedError(f"{label}格式应为 YYYY-MM-DD：{raw!r}") from exc


def _money(fen: int) -> str:
    return f"{fen / 100:.2f} 元"


def _call_key(ctx: RuntimeContext, tool_call_id: str | None, scope: str) -> str:
    return f"{ctx.run_id}:{tool_call_id or 'direct'}:{scope}"


def _order_summary(order: Order, *, room_type_name: str | None, replayed: bool = False) -> str:
    status = ORDER_STATUS_LABELS.get(order.status, order.status)
    room = room_type_name or "—"
    head = "该操作此前已处理，订单当前信息：" if replayed else "订单信息："
    return (
        f"{head}{order.order_no}，{room}，"
        f"{order.check_in_date.isoformat()} 至 {order.check_out_date.isoformat()}"
        f"（{order.nights} 晚），金额 {_money(order.amount_fen)}，状态：{status}。"
    )


async def _describe_order(session: AsyncSession, order_id: UUID, *, replayed: bool) -> str:
    order = await OrderRepository(session).get_by_id(order_id)
    if order is None:
        raise NotFoundError("order", order_id)
    room_type = await RoomTypeRepository(session).get_by_id(order.room_type_id)
    return _order_summary(
        order, room_type_name=room_type.name if room_type else None, replayed=replayed
    )


async def _describe_invoice(session: AsyncSession, invoice_id: UUID, *, replayed: bool) -> str:
    invoice = await InvoiceRepository(session).get_by_id(invoice_id)
    if invoice is None:
        raise NotFoundError("invoice", invoice_id)
    order = await OrderRepository(session).get_by_id(invoice.order_id)
    order_no = order.order_no if order else "—"
    status = INVOICE_STATUS_LABELS.get(invoice.status, invoice.status)
    head = "该申请此前已提交，发票当前信息：" if replayed else "发票申请已受理："
    return (
        f"{head}订单 {order_no}，抬头 {invoice.title}，"
        f"金额 {_money(invoice.amount_fen)}，状态：{status}。开具后可在小程序「订单」页查看。"
    )


async def _describe_result(
    session: AsyncSession, result_type: str, result_ref: UUID, *, replayed: bool
) -> str:
    if result_type == "invoice":
        return await _describe_invoice(session, result_ref, replayed=replayed)
    return await _describe_order(session, result_ref, replayed=replayed)


async def _run_write(
    ctx: RuntimeContext,
    runtime: ToolRuntime[RuntimeContext],
    *,
    scope: str,
    operation: Operation,
) -> str:
    """幂等受控的写用例：首次执行副作用，重复调用回放既有结果并复述当前状态。"""
    key = _call_key(ctx, getattr(runtime, "tool_call_id", None), scope)
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)

            async def bound() -> tuple[str, UUID]:
                return await operation(session, guest.id)

            outcome = await IdempotencyService(session).run(scope, key, bound)
            text = await _describe_result(
                session, outcome.result_type, outcome.result_ref, replayed=outcome.replayed
            )
            await session.commit()
    except DomainError as exc:
        return _domain_failure(exc)
    return text


@builtin_tool(category="order", read_only=True, display_name="房型列表")
async def list_room_types(runtime: ToolRuntime[RuntimeContext]) -> str:
    """列出酒店在售房型（代码、名称、每晚价格、可住人数与设施）。"""
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，房型查询不可用")
    async with ctx.session_factory() as session:
        room_types = await RoomTypeRepository(session).list_all()
    if not room_types:
        return "当前没有在售房型。"
    lines: list[str] = []
    for room_type in room_types:
        amenities = "、".join(room_type.amenities or [])
        suffix = f"，设施：{amenities}" if amenities else ""
        lines.append(
            f"- {room_type.code}：{room_type.name}，{_money(room_type.base_price_fen)}/晚，"
            f"可住 {room_type.capacity} 人，床型 {room_type.bed_type}{suffix}"
        )
    return "在售房型（预订时把房型代码传给 create_order）：\n" + "\n".join(lines)


@builtin_tool(category="order", read_only=True, display_name="我的订单")
async def list_my_orders(runtime: ToolRuntime[RuntimeContext]) -> str:
    """查看当前客人最近 5 笔订单（订单号、房型、日期、金额与状态）。"""
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单查询不可用")
    async with ctx.session_factory() as session:
        guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
        orders = await OrderRepository(session).list_recent_for_guest(
            guest.id, limit=RECENT_ORDER_LIMIT
        )
        if not orders:
            return "当前没有订单记录。"
        names = {
            room_type.id: room_type.name
            for room_type in await RoomTypeRepository(session).list_all(include_inactive=True)
        }
    lines = [
        _order_summary(order, room_type_name=names.get(order.room_type_id)) for order in orders
    ]
    return "最近订单：\n" + "\n".join(f"- {line.removeprefix('订单信息：')}" for line in lines)


@builtin_tool(category="order", read_only=False, display_name="创建预订")
async def create_order(
    room_type_code: str,
    check_in_date: str,
    check_out_date: str,
    runtime: ToolRuntime[RuntimeContext],
    guest_count: int | None = None,
) -> str:
    """为当前客人创建预订订单（创建后引导客人在小程序内完成支付）。

    room_type_code：房型代码，先用 list_room_types 查询；
    check_in_date / check_out_date：入住与退房日期，格式 YYYY-MM-DD；
    guest_count：入住人数（缺省 1）。
    调用前必须与客人确认房型、日期与人数，不要自行猜测日期。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单操作不可用")
    try:
        check_in = _parse_date(check_in_date, "入住日期")
        check_out = _parse_date(check_out_date, "退房日期")
    except ValidationFailedError as exc:
        return _domain_failure(exc)
    key = _call_key(ctx, getattr(runtime, "tool_call_id", None), "order.create")
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
            replayed = await OrderRepository(session).get_by_idempotency_key(key) is not None
            order = await OrderService(session).create_booking(
                guest_id=guest.id,
                room_type_code=room_type_code,
                check_in_date=check_in,
                check_out_date=check_out,
                guest_count=guest_count if guest_count is not None else 1,
                idempotency_key=key,
            )
            text = await _describe_order(session, order.id, replayed=replayed)
            await session.commit()
    except DomainError as exc:
        return _domain_failure(exc)
    if replayed:
        return text
    return f"预订已创建，请在支付后到店入住。{text}"


@builtin_tool(category="order", read_only=False, display_name="订单改期")
async def reschedule_order(
    order_no: str,
    new_check_in_date: str,
    new_check_out_date: str,
    runtime: ToolRuntime[RuntimeContext],
) -> str:
    """修改订单的入住与退房日期（改期前必须与客人确认新日期与价格变化）。

    order_no：订单号；new_check_in_date / new_check_out_date：新日期，格式 YYYY-MM-DD。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单操作不可用")
    try:
        check_in = _parse_date(new_check_in_date, "入住日期")
        check_out = _parse_date(new_check_out_date, "退房日期")
    except ValidationFailedError as exc:
        return _domain_failure(exc)

    async def operation(session: AsyncSession, guest_id: UUID) -> tuple[str, UUID]:
        order = await OrderService(session).reschedule_booking(
            guest_id=guest_id,
            order_no=order_no,
            new_check_in_date=check_in,
            new_check_out_date=check_out,
        )
        return ("order", order.id)

    return await _run_write(ctx, runtime, scope="order.reschedule", operation=operation)


@builtin_tool(category="order", read_only=False, display_name="订单续订")
async def renew_order(
    order_no: str,
    new_check_out_date: str,
    runtime: ToolRuntime[RuntimeContext],
) -> str:
    """延长已支付或已入住订单的退房日期（续订前与客人确认新退房日期）。

    order_no：订单号；new_check_out_date：新的退房日期，格式 YYYY-MM-DD。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单操作不可用")
    try:
        check_out = _parse_date(new_check_out_date, "退房日期")
    except ValidationFailedError as exc:
        return _domain_failure(exc)

    async def operation(session: AsyncSession, guest_id: UUID) -> tuple[str, UUID]:
        order = await renew_booking_with_credential(
            session,
            guest_id=guest_id,
            order_no=order_no,
            new_check_out_date=check_out,
        )
        return ("order", order.id)

    return await _run_write(ctx, runtime, scope="order.renew", operation=operation)


@builtin_tool(category="order", read_only=False, display_name="取消订单")
async def cancel_order(
    order_no: str,
    runtime: ToolRuntime[RuntimeContext],
    reason: str | None = None,
) -> str:
    """取消未入住订单（涉及退款诉求时先转人工确认再操作）。

    order_no：订单号；reason：取消原因（可省略）。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单操作不可用")

    async def operation(session: AsyncSession, guest_id: UUID) -> tuple[str, UUID]:
        order = await OrderService(session).cancel_booking(
            guest_id=guest_id, order_no=order_no, reason=reason
        )
        return ("order", order.id)

    return await _run_write(ctx, runtime, scope="order.cancel", operation=operation)


@builtin_tool(category="order", read_only=False, display_name="发票申请")
async def apply_invoice(
    order_no: str,
    title: str,
    runtime: ToolRuntime[RuntimeContext],
    tax_no: str | None = None,
) -> str:
    """为已完成支付的订单申请发票（同一订单至多一张，重复申请返回既有发票）。

    order_no：订单号；title：发票抬头；tax_no：税号（个人抬头可省略）。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，订单操作不可用")
    if not title.strip():
        return _domain_failure(ValidationFailedError("发票抬头不能为空"))

    async def operation(session: AsyncSession, guest_id: UUID) -> tuple[str, UUID]:
        invoice = await OrderService(session).apply_invoice(
            guest_id=guest_id,
            order_no=order_no,
            title=title.strip(),
            tax_no=tax_no.strip() if tax_no else None,
        )
        return ("invoice", invoice.id)

    return await _run_write(ctx, runtime, scope="invoice.apply", operation=operation)
