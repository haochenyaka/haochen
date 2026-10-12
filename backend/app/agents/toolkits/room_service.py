"""客房服务工具：呼叫配送、呼叫清洁、投诉。

写类工具带幂等键（Run + 工具调用 ID），worker 恢复重放不产生第二次副作用。
"""

from __future__ import annotations

import logging
from uuid import UUID

from langgraph.prebuilt import ToolRuntime

from app.agents.runtime import RuntimeContext
from app.agents.toolkits.registry import builtin_tool
from app.domain.errors import DomainError
from app.repositories import GuestRepository, RoomRepository
from app.services.complaint_service import ComplaintService
from app.services.housekeeping_service import HousekeepingService
from app.services.idempotency_service import IdempotencyService
from app.services.robot_delivery_service import RobotDeliveryService

logger = logging.getLogger(__name__)


def _failure(reason: str) -> str:
    logger.warning("客房服务工具失败：%s", reason)
    return f"操作失败：{reason}"


def _domain_failure(exc: DomainError) -> str:
    logger.warning("客房服务工具失败：%s: %s", type(exc).__name__, exc)
    return f"操作失败：{exc}"


def _call_key(ctx: RuntimeContext, tool_call_id: str | None, scope: str) -> str:
    return f"{ctx.run_id}:{tool_call_id or 'direct'}:{scope}"


@builtin_tool(category="room_service", display_name="呼叫配送")
async def request_robot_delivery(
    item_name: str,
    runtime: ToolRuntime[RuntimeContext],
    room_no: str = "",
    item_quantity: int = 1,
) -> str:
    """请求机器人配送物品到房间。告知客人可配送的物品范围，确认物品名称和数量后调用。

    Args:
        item_name: 物品名称
        room_no: 房间号
        item_quantity: 数量，默认 1
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，配送不可用")
    if not item_name:
        return "请告知需要配送什么物品。"
    if not room_no:
        return "请提供房间号。"
    key = _call_key(ctx, getattr(runtime, "tool_call_id", None), "robot_delivery")
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)

            async def bound() -> tuple[str, UUID]:
                delivery = await RobotDeliveryService(session).create(
                    guest_id=guest.id,
                    room_no=room_no,
                    item={"name": item_name, "quantity": item_quantity},
                    idempotency_key=key,
                )
                return "robot_delivery", delivery.id

            outcome = await IdempotencyService(session).run("room_service", key, bound)
            await session.commit()
            if outcome.replayed:
                return (
                    f"此配送请求此前已提交。机器人将尽快配送"
                    f"{_qty(item_quantity)}{item_name}到 {room_no} 房间。"
                )
            return (
                f"配送请求已提交。机器人即将配送"
                f"{_qty(item_quantity)}{item_name}到 {room_no} 房间，请留意接收。"
            )
    except DomainError as exc:
        return _domain_failure(exc)


@builtin_tool(category="room_service", display_name="呼叫清洁")
async def request_room_clean(
    room_no: str,
    runtime: ToolRuntime[RuntimeContext],
    note: str = "",
) -> str:
    """请求保洁人员上门清洁房间。

    Args:
        room_no: 房间号
        note: 清洁说明，可选
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，清洁服务不可用")
    if not room_no:
        return "请提供房间号。"
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
            room = await RoomRepository(session).get_by_room_no(room_no)
            if room is None:
                return f"房间 {room_no} 不存在，请确认房间号。"
            payload = {"requested_by": str(guest.id)}
            if note:
                payload["note"] = note
            await HousekeepingService(session).create(
                task_type="call_clean",
                room_id=room.id,
                room_no=room.room_no,
                payload=payload,
            )
            await session.commit()
            suffix = f"，备注：{note}" if note else ""
            return f"呼叫清洁已提交{suffix}。保洁人员将尽快前往 {room_no} 房间，请稍候。"
    except DomainError as exc:
        return _domain_failure(exc)


@builtin_tool(category="room_service", display_name="投诉")
async def submit_complaint(
    content: str,
    runtime: ToolRuntime[RuntimeContext],
    category: str = "",
) -> str:
    """提交投诉或意见反馈。

    Args:
        content: 投诉内容
        category: 类别：noise(噪音)/cleanliness(卫生)/facility(设施)/service(服务)/other(其他)
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，投诉不可用")
    if not content:
        return "请描述投诉内容。"
    try:
        async with ctx.session_factory() as session:
            guest = await GuestRepository(session).get_or_create_by_user(ctx.user_id)
            await ComplaintService(session).create(
                guest_id=guest.id,
                content=content,
                category=category.strip() or None,
            )
            await session.commit()
            return "投诉已提交，我们会尽快处理并回复。感谢您的反馈。"
    except DomainError as exc:
        return _domain_failure(exc)


def _qty(quantity: int) -> str:
    return f"{quantity} 份 " if quantity > 1 else ""