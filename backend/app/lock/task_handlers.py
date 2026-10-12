"""凭证任务处理器：入住签发（checkin.prepare v1）与每日轮换（lock.rotate_daily v1）。

不变量：
- 任务结果只含元数据（房间号、版本、生效窗口），不含明文 code（结果随任务落库）；明文
  只在签发/轮换时刻存在，后续下发由调用方按需经凭证通道获取（A2）；
- 入住处理幂等：`checkin:{order_id}` 幂等键命中既有凭证即返回，不重复签发改密；明文不可
  回读，重放也不再触达客人（通知侧按"无明文"跳过）；
- 失败钩子把失败事实追加为订单事件（checkin_blocked），订单不静默进入后续状态；
- 入住通知（微信订阅消息）是第二通道：失败不重试、不改任务结果，结果以订单事件留痕；
- 每日轮换逐房独立事务：已生效的房间不回滚；设备不可达立即停止后续房间（不在故障设备上
  空转），存在失败房间即任务失败关闭，等待人工确认后重跑（轮换幂等，重跑安全）；
- 轮换提交每天至多一次（去重键 `lock.rotate_daily:{date}`，含终态判定）：失败日不自动重放，
  避免设备长时间不可达时把下一次 cron 变成无限重试。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from contextlib import suppress
from datetime import date, datetime, time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import SessionFactory
from app.domain.errors import ConflictError, NotFoundError
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock.client import LockClient, LockError, LockUnavailableError
from app.lock.service import CredentialFact, LockCredentialService, RoomRotation
from app.repositories.order_repository import OrderRepository
from app.repositories.task_repository import TaskRepository
from app.services.checkin_notice_service import NoticeOutcome, notify_checkin
from app.services.task_dispatch import TaskDispatcher, dispatch_task
from app.services.task_registry import TaskContext, task_handler
from app.services.task_service import (
    TaskService,
    TaskSubmission,
    context_dispatcher,
    context_session_factory,
)
from app.wechat import WechatClient

logger = logging.getLogger(__name__)

CHECKIN_TASK_TYPE = "checkin.prepare"
CHECKIN_HANDLER_VERSION = 1
CHECKIN_BLOCKED_EVENT = "checkin_blocked"
MAX_EVENT_REASON_CHARS = 200


class CheckinTaskError(Exception):
    """本次执行不可继续：载荷或订单状态不允许，任务据此失败关闭。"""


def _order_id(ctx: TaskContext) -> UUID | None:
    raw = ctx.payload_value("order_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


async def _checkin_failure(session: AsyncSession, ctx: TaskContext, error: str) -> None:
    """失败收敛：把 checkin_blocked 事件追加到订单（与任务终态同事务）。"""
    order_id = _order_id(ctx)
    if order_id is None:
        return
    orders = OrderRepository(session)
    order = await orders.get_by_id(order_id)
    if order is None:
        return
    written = await orders.apply_fields(
        order.id,
        from_statuses=(order.status,),
        values={},
        event={"type": CHECKIN_BLOCKED_EVENT, "reason": error[:MAX_EVENT_REASON_CHARS]},
    )
    if not written:
        logger.warning("checkin_blocked 事件写入未命中（订单状态并发改写）：order_id=%s", order_id)


async def _notify_guest(ctx: TaskContext, *, order_id: UUID, fact: CredentialFact) -> NoticeOutcome:
    """入住通知为第二通道：任何失败只记事件与日志，不影响任务结果、不重试。"""
    client = WechatClient.from_settings()
    try:
        async with ctx.session_factory() as session:
            outcome = await notify_checkin(session, order_id=order_id, fact=fact, client=client)
            await session.commit()
    except Exception:
        logger.warning("入住通知流程异常（主通道不受影响）：order_id=%s", order_id, exc_info=True)
        return NoticeOutcome(status="failed", reason="通知流程异常（详见服务日志）")
    finally:
        await client.aclose()
    return outcome


@task_handler(CHECKIN_TASK_TYPE, CHECKIN_HANDLER_VERSION, on_failure=_checkin_failure)
async def prepare_checkin(ctx: TaskContext) -> Mapping[str, Any]:
    order_id = _order_id(ctx)
    if order_id is None:
        raise CheckinTaskError("checkin.prepare 缺少 order_id 载荷")

    client = LockClient.from_settings()
    try:
        async with ctx.session_factory() as session:
            fact = await LockCredentialService(session).issue_for_order(
                order_id=order_id, client=client
            )
            await session.commit()
    finally:
        await client.aclose()

    outcome = await _notify_guest(ctx, order_id=order_id, fact=fact)
    logger.info(
        "checkin.prepare 完成：order_id=%s credential_version=%d notify=%s",
        order_id,
        fact.version,
        outcome.status,
    )
    return {
        "order_id": str(order_id),
        "room_no": fact.room_no,
        "floor": fact.floor,
        "credential_version": fact.version,
        "valid_from": fact.valid_from.isoformat(),
        "valid_to": fact.valid_to.isoformat(),
        "replayed": fact.replayed,
        "notify_status": outcome.status,
    }


ROTATE_DAILY_TASK_TYPE = "lock.rotate_daily"
ROTATE_DAILY_HANDLER_VERSION = 1
ROTATE_DAILY_DEDUPE_PREFIX = "lock.rotate_daily:"
ROTATE_START_LOCAL = time(0, 5)
"""每日轮换最早执行时刻（酒店本地）：cron 全天轮询，早于此时刻不提交。"""

MAX_SKIP_REASON_CHARS = 200
FAILED_DETAIL_CHARS = 1500


class LockRotationError(Exception):
    """轮换批次存在失败房间：任务失败关闭（摘要只含元数据，逐房明细在日志/错误文本）。"""


def daily_rotation_dedupe_key(rotate_date: date) -> str:
    return f"{ROTATE_DAILY_DEDUPE_PREFIX}{rotate_date.isoformat()}"


def _rotate_date(ctx: TaskContext) -> date:
    raw = ctx.payload_value("rotate_date")
    if isinstance(raw, str):
        with suppress(ValueError):
            return date.fromisoformat(raw)
    return datetime.now(HOTEL_TIMEZONE).date()


def _rotation_metadata(rotation: RoomRotation) -> dict[str, Any]:
    return {
        "room_no": rotation.room_no,
        "promoted": rotation.promoted,
        "version": rotation.version,
        "valid_from": rotation.valid_from.isoformat(),
        "valid_to": rotation.valid_to.isoformat(),
        "next_version": rotation.next_version,
        "next_valid_from": rotation.next_valid_from.isoformat(),
        "next_valid_to": rotation.next_valid_to.isoformat(),
    }


@task_handler(ROTATE_DAILY_TASK_TYPE, ROTATE_DAILY_HANDLER_VERSION)
async def rotate_daily_credentials(ctx: TaskContext) -> Mapping[str, Any]:
    """每日轮换：到点 next 转正 + 今日凭证兜底签发 + 预排次日 next（逐房独立事务）。

    在住/在途与维保房跳过（跳过原因随结果留痕）；执行处复验的并发冲突按跳过处理，
    其余异常按失败处理。设备不可达后不再触碰后续房间，避免把故障放大成整批噪声。
    """
    rotate_date = _rotate_date(ctx)
    client = LockClient.from_settings()
    rotated: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    device_down = False
    try:
        async with ctx.session_factory() as session:
            plan = await LockCredentialService(session).plan_daily_rotation(today=rotate_date)
        skipped.extend(
            {"room_no": room_no, "reason": "occupied"} for room_no in plan.skipped_occupied
        )
        skipped.extend(
            {"room_no": room_no, "reason": "maintenance"} for room_no in plan.skipped_maintenance
        )
        for room_no in plan.candidates:
            if device_down:
                skipped.append({"room_no": room_no, "reason": "device_unavailable"})
                continue
            try:
                async with ctx.session_factory() as session:
                    rotation = await LockCredentialService(session).rotate_daily_room(
                        room_no=room_no, client=client
                    )
                    await session.commit()
            except LockUnavailableError as exc:
                device_down = True
                failed.append({"room_no": room_no, "reason": "device_unavailable"})
                logger.warning(
                    "每日轮换设备不可达，停止后续房间：room_no=%s error=%s", room_no, exc
                )
            except LockError as exc:
                failed.append(
                    {"room_no": room_no, "reason": f"lock_error status={exc.status_code}"}
                )
                logger.warning("每日轮换门锁拒绝：room_no=%s error=%s", room_no, exc)
            except (ConflictError, NotFoundError) as exc:
                skipped.append({"room_no": room_no, "reason": str(exc)[:MAX_SKIP_REASON_CHARS]})
            except Exception:
                failed.append({"room_no": room_no, "reason": "unexpected_error"})
                logger.warning("每日轮换房间异常：room_no=%s", room_no, exc_info=True)
            else:
                rotated.append(_rotation_metadata(rotation))
    finally:
        await client.aclose()

    logger.info(
        "lock.rotate_daily 完成：date=%s rotated=%d skipped=%d failed=%d",
        rotate_date,
        len(rotated),
        len(skipped),
        len(failed),
    )
    if failed:
        detail = json.dumps({"failed": failed, "skipped": skipped}, ensure_ascii=False)
        raise LockRotationError(
            f"每日轮换存在失败房间：date={rotate_date.isoformat()} failed={len(failed)} "
            f"skipped={len(skipped)} detail={detail[:FAILED_DETAIL_CHARS]}"
        )
    return {
        "rotate_date": rotate_date.isoformat(),
        "rotated": rotated,
        "skipped": skipped,
    }


async def submit_rotation(
    session: AsyncSession,
    *,
    rotate_date: date,
    dispatcher: TaskDispatcher | None = None,
) -> TaskSubmission:
    """在调用方事务内提交某日轮换并投递：去重键命中活动任务时复用（人工重跑失败日即新执行）。"""
    submission = await TaskService(session).submit(
        task_type=ROTATE_DAILY_TASK_TYPE,
        handler_version=ROTATE_DAILY_HANDLER_VERSION,
        payload={"rotate_date": rotate_date.isoformat()},
        dedupe_key=daily_rotation_dedupe_key(rotate_date),
    )
    await session.commit()
    if submission.should_dispatch:
        await dispatch_task(dispatcher, submission.task_id)
    return submission


async def submit_daily_rotation(
    *,
    rotate_date: date,
    dispatcher: TaskDispatcher | None = None,
    session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
) -> TaskSubmission:
    """自持事务的提交入口：cron 与恢复流程用（无请求会话）。"""
    async with session_factory() as session:
        return await submit_rotation(session, rotate_date=rotate_date, dispatcher=dispatcher)


async def submit_daily_rotation_job(ctx: dict[str, Any]) -> str | None:
    """cron 入口：酒店本地 00:05 起首次触发时提交当日轮换（每天至多提交一次）。

    任意状态的任务行都算「已提交」：失败日不自动重放，由人工确认后显式重跑。
    """
    moment = datetime.now(HOTEL_TIMEZONE)
    if moment.time() < ROTATE_START_LOCAL:
        return None
    rotate_date = moment.date()
    factory = context_session_factory(ctx)
    async with factory() as session:
        if await TaskRepository(session).exists_by_dedupe_key(
            daily_rotation_dedupe_key(rotate_date)
        ):
            return None
    submission = await submit_daily_rotation(
        rotate_date=rotate_date,
        dispatcher=context_dispatcher(ctx),
        session_factory=factory,
    )
    logger.info(
        "lock.rotate_daily 已提交：date=%s task_id=%s deduplicated=%s",
        rotate_date,
        submission.task_id,
        submission.deduplicated,
    )
    return str(submission.task_id)
