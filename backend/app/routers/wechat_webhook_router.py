"""微信支付回调：验签 → 先提交 PostgreSQL 事实 → 再投递入住准备任务（契约 §4.2）。

不变量：
- 验签失败 401 且不写任何事实；金额不符 400 且不写任何事实；
- 重复回调命中 transaction_id 幂等键即返回成功，不产生第二单（A3）；
- 支付事实与入住准备任务在同一事务提交，投递在提交之后进行，失败由恢复补发；
- 乱序回调不回退已 paid 的订单状态（服务层状态机守卫）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.services.payment_service import (
    PaymentAmountMismatchError,
    PaymentCallbackBody,
    PaymentService,
)
from app.services.task_dispatch import NullTaskDispatcher, TaskDispatcher, dispatch_task
from app.services.task_service import TaskService
from app.wechat import verify_callback_signature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/wechat", tags=["webhooks"])

CHECKIN_TASK_TYPE = "checkin.prepare"
CHECKIN_HANDLER_VERSION = 1

_fallback_dispatcher = NullTaskDispatcher()


def get_task_dispatcher(request: Request) -> TaskDispatcher:
    dispatcher: TaskDispatcher | None = getattr(request.app.state, "task_dispatcher", None)
    return dispatcher if dispatcher is not None else _fallback_dispatcher


class PayCallbackBody(BaseModel):
    event: Literal["success", "fail"]
    order_no: str = Field(min_length=1, max_length=32)
    transaction_id: str = Field(min_length=1, max_length=64)
    amount_fen: int = Field(ge=0)
    openid: str = Field(min_length=1, max_length=64)
    notified_at: str | None = None


@router.post("/pay")
async def wechat_pay_callback(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    dispatcher: Annotated[TaskDispatcher, Depends(get_task_dispatcher)],
) -> dict[str, str]:
    raw = await request.body()
    signature = request.headers.get("X-Sim-Signature")
    if not verify_callback_signature(
        raw, signature, secret=get_settings().device_sim_signing_secret
    ):
        remote = request.client.host if request.client else "-"
        logger.warning("支付回调验签失败，拒绝记账：remote=%s", remote)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="签名校验失败")

    try:
        body = PayCallbackBody.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="回调体不合约"
        ) from exc

    service = PaymentService(session)
    try:
        result = await service.apply_callback(
            PaymentCallbackBody(
                event=body.event,
                order_no=body.order_no,
                transaction_id=body.transaction_id,
                amount_fen=body.amount_fen,
                openid=body.openid,
            )
        )
    except PaymentAmountMismatchError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    submission = None
    if result.checkin_required:
        submission = await TaskService(session).submit(
            task_type=CHECKIN_TASK_TYPE,
            handler_version=CHECKIN_HANDLER_VERSION,
            payload={"order_id": str(result.order_id), "payment_id": str(result.payment_id)},
            dedupe_key=f"{CHECKIN_TASK_TYPE}:{result.order_id}",
        )
    await session.commit()
    if submission is not None and submission.should_dispatch:
        await dispatch_task(dispatcher, submission.task_id)
    return {"code": "SUCCESS", "status": result.status}
