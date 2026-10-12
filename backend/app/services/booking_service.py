"""预订写用例组合层：订单变更 + 门锁侧连带动作收在一处，Agent 工具与 C 端 REST 共用。

不变量：
- 续订先改订单事实、再展期凭证窗口；门锁不可达即失败关闭（调用方事务不提交），
  不出现「订单已续、密码失效」的分叉；
- 组合层只编排，不复验业务规则：归属、状态、日期窗、容量与库存由 OrderService 执行处复验；
- 事务边界归调用方（工具在幂等包装内提交，REST 在请求会话内提交）。
"""

from __future__ import annotations

import logging
from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError
from app.lock.client import LockClient, LockError
from app.lock.service import LockCredentialService
from app.models.domain import Order
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)


async def renew_booking_with_credential(
    session: AsyncSession,
    *,
    guest_id: UUID,
    order_no: str,
    new_check_out_date: date,
) -> Order:
    """续订：延长退房日期并保持当前凭证（只展期窗口，不换码、不换版本）。"""
    order = await OrderService(session).renew_booking(
        guest_id=guest_id, order_no=order_no, new_check_out_date=new_check_out_date
    )
    await extend_credential_window(session, order)
    return order


async def extend_credential_window(session: AsyncSession, order: Order) -> None:
    """把当前凭证的生效终点展到续订后的住宿窗口；门锁失败即抛 ConflictError（失败关闭）。"""
    client = LockClient.from_settings()
    try:
        window = await LockCredentialService(session).extend_for_renewal(order=order, client=client)
    except LockError as exc:
        logger.warning("续订展期失败（门锁侧）：order_no=%s error=%s", order.order_no, exc)
        raise ConflictError("门锁服务暂时不可用，续订未生效，请稍后重试") from exc
    finally:
        await client.aclose()
    if window is not None and window.extended:
        logger.info(
            "续订展期完成：order_no=%s room_no=%s valid_to=%s",
            order.order_no,
            window.room_no,
            window.valid_to.isoformat(),
        )
