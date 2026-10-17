"""入住（checkin-flow Skill）工具：入住信息只读查询与开门密码按下发。

归属校验在执行处再次收口：只处理当前客人自己的订单，他人订单按"不存在"口径回应。
下发即换一次性新码（明文不可回读，A2）：工具回包里的明文由 CredentialGuard 取走并入
下一次模型请求，落库副本一律打码；重复下发由 ToolGuard 按同参复用，不产生第二次轮换。
"""

from __future__ import annotations

import logging
from datetime import datetime

from langgraph.prebuilt import ToolRuntime

from app.agents.runtime import RuntimeContext
from app.agents.toolkits.orders import ORDER_NOT_FOUND_TEXT, ORDER_STATUS_LABELS
from app.agents.toolkits.registry import builtin_tool
from app.domain.errors import DomainError, NotFoundError, PermissionDeniedError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock.client import LockClient
from app.lock.service import CheckinOverview, LockCredentialService
from app.repositories import GuestRepository

logger = logging.getLogger(__name__)

CHECKIN_NOT_READY_TEXT = "入住凭证尚未就绪，请稍后重试或联系前台。"


def _failure(reason: str) -> str:
    logger.warning("入住工具失败：%s", reason)
    return f"操作失败：{reason}"


def _domain_failure(exc: DomainError) -> str:
    """业务失败 → 模型可见文本：归属/不存在统一措辞，不泄露订单是否存在。"""
    logger.warning("入住工具失败：%s: %s", type(exc).__name__, exc)
    if isinstance(exc, (NotFoundError, PermissionDeniedError)):
        return ORDER_NOT_FOUND_TEXT
    return f"操作失败：{exc}"


def _local_time(moment: datetime) -> str:
    return moment.astimezone(HOTEL_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def _describe_overview(overview: CheckinOverview) -> str:
    order = overview.order
    status = ORDER_STATUS_LABELS.get(order.status, order.status)
    head = (
        f"入住信息（订单 {order.order_no}，"
        f"{order.check_in_date.isoformat()} 至 {order.check_out_date.isoformat()}，"
        f"状态：{status}）："
    )
    if overview.room_no is None or overview.credential is None:
        return f"{head}\n- 房间与门锁凭证尚未就绪；如客人已支付，请稍后重试或联系前台。"
    credential = overview.credential
    return (
        f"{head}\n"
        f"- 房间号：{overview.room_no}，楼层：{overview.floor} 层\n"
        f"- 门锁凭证有效期：{_local_time(credential.valid_from)} 至 "
        f"{_local_time(credential.valid_to)}\n"
        f"- 这里不含开门密码本身；客人索要密码时用 deliver_room_password 下发。"
    )


@builtin_tool(category="checkin", skill="checkin-flow", read_only=True, display_name="入住信息")
async def get_checkin_info(order_no: str, runtime: ToolRuntime[RuntimeContext]) -> str:
    """查询客人自己订单的入住信息：房间号、楼层与门锁凭证有效期（不含开门密码）。

    order_no：订单号；客人未提供时先向其索要，不要凭房间号猜测；
    客人索要开门密码时改用 deliver_room_password（每次下发都会换新密码）。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，入住信息查询不可用")
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
            overview = await LockCredentialService(session).overview_for_order(
                guest_id=guest.id, order_no=order_no.strip()
            )
    except DomainError as exc:
        return _domain_failure(exc)
    return _describe_overview(overview)


@builtin_tool(
    category="checkin",
    skill="checkin-flow",
    read_only=False,
    credential_delivery=True,
    display_name="下发开门密码",
)
async def deliver_room_password(order_no: str, runtime: ToolRuntime[RuntimeContext]) -> str:
    """为客人已支付的订单重新生成开门密码并交给当次回复下发。

    每次调用都会生成新密码、旧密码立即失效；密码仅在当次回复中展示，历史记录里打码。
    order_no：订单号；客人未提供时先向其索要，可用 get_checkin_info 核对。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，密码下发不可用")
    client = LockClient.from_settings()
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
            fact = await LockCredentialService(session).deliver_for_order(
                guest_id=guest.id, order_no=order_no.strip(), client=client
            )
            await session.commit()
    except DomainError as exc:
        return _domain_failure(exc)
    finally:
        await client.aclose()
    if fact.code is None:
        return CHECKIN_NOT_READY_TEXT
    return (
        f"开门密码已重新生成：密码：{fact.code}\n"
        f"- 房间号：{fact.room_no}，楼层：{fact.floor} 层\n"
        f"- 有效期至：{_local_time(fact.valid_to)}\n"
        f"- 旧密码已立即失效；本密码仅本次展示，请客人及时保存。"
    )
