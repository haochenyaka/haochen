"""门锁凭证用例服务：房间分配、签发、轮换、吊销、每日轮换、续订保持与开锁记录（A2：明文零落库）。

不变量：
- 明文 code 只在签发/轮换/预生成的返回载荷中存在；落库只有 code_hash 与生效窗口；
- code_hash 是 keyed HMAC（LOCK_MASTER_KEY 派生），非裸散列，防离线暴力枚举；
- 入住签发以 `checkin:{order_id}` 幂等键去重：重复执行返回既有事实、不二次改密；
- 房间分配取房型内与本订单日期窗无重叠占用的房间；分配与签发同一事务；
- 轮换/吊销携带期望版本：与本地当前版本不符即拒绝（防旧消息重放改密，契约 §2.3）；
- 每日轮换只动空置房：在住/在途（已支付、客人已持码）与维保房间跳过；到点的次日凭证
  转正、当前凭证当日窗口过期即作废；续订保持当前凭证只延长窗口、不换码。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.domain.errors import ConflictError, NotFoundError
from app.domain.states import ORDER_STAY_STATUSES
from app.domain.vocabulary import HOTEL_TIMEZONE
from app.lock.client import (
    IssuedCredential,
    LockClient,
    LockError,
    UnlockRecord,
)
from app.models.domain import LockCredential, Order, Room
from app.repositories.lock_credential_repository import LockCredentialRepository
from app.repositories.order_repository import OrderRepository
from app.repositories.room_repository import RoomRepository

logger = logging.getLogger(__name__)

CHECKIN_STATUSES: frozenset[str] = ORDER_STAY_STATUSES
"""可签发/复用入住凭证的订单状态：payment 之后、完成之前。"""

ROOM_BLOCKED_STATUSES: frozenset[str] = frozenset({"maintenance"})
"""不参与分配与每日轮换的房间状态：维修中不可住；清洁中仍可被未来日期窗分配。"""

ROOM_IN_HOUSE_STATUS = "occupied"
"""房态口径的"房内有人"：与住宿存续订单口径并行，任一命中都跳过每日轮换。"""

MIN_VALID_HOURS = 24
DAY = timedelta(days=1)
EXTEND_TOLERANCE = timedelta(minutes=1)
"""续订展期比较容差：窗口已覆盖（或仅差秒级）时不触达设备，保证重放幂等。"""


@dataclass(frozen=True)
class CredentialFact:
    """凭证事实：`code` 仅在签发时刻的返回载荷中存在，调用方禁止持久化与打印。"""

    order_id: UUID | None
    room_id: UUID
    room_no: str
    floor: int
    credential_id: UUID
    device_credential_id: str | None
    version: int
    reason: str
    valid_from: datetime
    valid_to: datetime
    replayed: bool
    code: str | None = None


@dataclass(frozen=True)
class CheckinOverview:
    """入住信息只读视图：房间分配与凭证均未就绪时对应字段为 None，永不携带明文。"""

    order: Order
    room_no: str | None
    floor: int | None
    credential: CredentialFact | None


@dataclass(frozen=True)
class RotationPlan:
    """每日轮换计划：候选与跳过清单（房间号）；跳过原因由房态与住宿存续订单派生。"""

    candidates: tuple[str, ...]
    skipped_occupied: tuple[str, ...]
    skipped_maintenance: tuple[str, ...]


@dataclass(frozen=True)
class RoomRotation:
    """单房间轮换结果：只有元数据（房间号、动作、版本与窗口），无明文。"""

    room_no: str
    promoted: bool
    version: int
    valid_from: datetime
    valid_to: datetime
    next_version: int
    next_valid_from: datetime
    next_valid_to: datetime


@dataclass(frozen=True)
class CredentialWindow:
    """凭证窗口事实（续订展期）：无明文；`extended=False` 表示窗口已覆盖、未调用设备。"""

    room_no: str
    version: int
    valid_from: datetime
    valid_to: datetime
    extended: bool


def hash_lock_code(*, room_no: str, version: int, code: str, key_version: int = 1) -> str:
    """门锁密码散列：keyed HMAC-SHA256，库中不存明文（A2）。"""
    raw = f"{room_no}:{version}:{key_version}:{code}"
    return hmac.new(
        get_settings().lock_master_key.encode(), raw.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def checkin_idempotency_key(order_id: UUID) -> str:
    return f"checkin:{order_id}"


class LockCredentialService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._orders = OrderRepository(session)
        self._rooms = RoomRepository(session)
        self._credentials = LockCredentialRepository(session)

    async def issue_for_order(self, *, order_id: UUID, client: LockClient) -> CredentialFact:
        order = await self._orders.get_by_id(order_id)
        if order is None:
            raise NotFoundError("order", order_id)
        if order.status not in CHECKIN_STATUSES:
            raise ConflictError(f"订单当前状态（{order.status}）不能签发入住凭证")

        key = checkin_idempotency_key(order.id)
        existing = await self._credentials.get_by_idempotency_key(key)
        if existing is not None:
            # 幂等命中回放当下有效凭证：签发后被下发轮换时，键命中行已吊销，取 active 行
            room = await self._rooms.get_by_id(existing.room_id)
            floor = room.floor if room else 0
            active = await self._credentials.get_active_for_room(existing.room_no)
            return _fact(active or existing, order_id=order.id, floor=floor, replayed=True)

        room = await self._assign_room(order)
        issued = await client.issue_credential(
            room_no=room.room_no, valid_hours=_valid_hours(order), reason="checkin"
        )
        return await self._persist(
            order=order,
            room_id=room.id,
            room_no=room.room_no,
            floor=room.floor,
            issued=issued,
            reason="checkin",
            idempotency_key=key,
        )

    async def overview_for_order(self, *, guest_id: UUID, order_no: str) -> CheckinOverview:
        """入住信息只读：归属校验沿用「不存在」口径，不泄露订单是否存在。"""
        order = await self._owned_order(guest_id=guest_id, order_no=order_no)
        if order.room_id is None:
            return CheckinOverview(order=order, room_no=None, floor=None, credential=None)
        room = await self._rooms.get_by_id(order.room_id)
        if room is None:
            return CheckinOverview(order=order, room_no=None, floor=None, credential=None)
        active = await self._credentials.get_active_for_room(room.room_no)
        return CheckinOverview(
            order=order,
            room_no=room.room_no,
            floor=room.floor,
            credential=_fact(active, order_id=order.id, floor=room.floor, replayed=False)
            if active is not None
            else None,
        )

    async def deliver_for_order(
        self, *, guest_id: UUID, order_no: str, client: LockClient
    ) -> CredentialFact:
        """按需下发开门密码：以当前 active 凭证为期望版本轮换，明文只进返回载荷。

        明文不可回读（A2），下发即换一次性新码：旧码在设备与本地同时失效；携带期望
        版本，旧消息重放无法改密。凭证未就绪（未支付或入住准备未完成）时失败关闭。
        """
        order = await self._owned_order(guest_id=guest_id, order_no=order_no)
        if order.status not in CHECKIN_STATUSES:
            raise ConflictError(f"订单当前状态（{order.status}）不能下发开门密码")
        if order.room_id is None:
            raise ConflictError("入住凭证尚未就绪，请稍后重试或联系前台")
        room = await self._rooms.get_by_id(order.room_id)
        if room is None:
            raise ConflictError("入住凭证尚未就绪，请稍后重试或联系前台")
        current = await self._credentials.get_active_for_room(room.room_no)
        if current is None:
            raise ConflictError("入住凭证尚未就绪，请稍后重试或联系前台")

        issued = await client.rotate_credential(
            room_no=room.room_no,
            valid_hours=_valid_hours(order),
            reason="delivery",
            expected_version=current.version,
        )
        if not await self._credentials.revoke(current.id):
            raise ConflictError(f"凭证并发改写，下发未生效：{room.room_no}")
        return await self._persist(
            order=order,
            room_id=room.id,
            room_no=room.room_no,
            floor=room.floor,
            issued=issued,
            reason="delivery",
            idempotency_key=None,
            event_type="credential_delivered",
        )

    async def _owned_order(self, *, guest_id: UUID, order_no: str) -> Order:
        order = await self._orders.get_by_order_no(order_no)
        if order is None or order.guest_id != guest_id:
            raise NotFoundError("order", order_no)
        return order

    async def rotate(
        self,
        *,
        room_no: str,
        client: LockClient,
        expected_version: int | None = None,
        reason: str = "daily",
        valid_hours: int = MIN_VALID_HOURS,
    ) -> CredentialFact:
        room = await self._rooms.get_by_room_no(room_no)
        if room is None:
            raise NotFoundError("room", room_no)
        current = await self._credentials.get_active_for_room(room_no)
        if current is None:
            raise NotFoundError("lock_credential", room_no)
        _ensure_expected_version(current, expected_version)

        issued = await client.rotate_credential(
            room_no=room_no,
            valid_hours=valid_hours,
            reason=reason,
            expected_version=current.version,
        )
        if not await self._credentials.revoke(current.id):
            raise ConflictError(f"凭证并发改写，轮换未生效：{room_no}")
        return await self._persist(
            order=None,
            room_id=room.id,
            room_no=room_no,
            floor=room.floor,
            issued=issued,
            reason=reason,
            idempotency_key=None,
        )

    async def revoke(
        self,
        *,
        room_no: str,
        client: LockClient,
        expected_version: int | None = None,
    ) -> UUID:
        current = await self._credentials.get_active_for_room(room_no)
        if current is None:
            raise NotFoundError("lock_credential", room_no)
        _ensure_expected_version(current, expected_version)

        try:
            await client.revoke_credential(room_no=room_no, expected_version=current.version)
        except LockError as exc:
            # 设备侧已无凭证（404）：本地仍有 active 行属状态分叉，仍吊销本地并告警
            if exc.status_code != 404:
                raise
            logger.warning("门锁侧无凭证但本地 active 存在，继续吊销本地：room_no=%s", room_no)
        if not await self._credentials.revoke(current.id):
            raise ConflictError(f"凭证并发改写，吊销未生效：{room_no}")
        return current.id

    async def plan_daily_rotation(self, *, today: date) -> RotationPlan:
        """每日轮换计划：在住/在途订单的房间与维保房跳过，其余为空置候选（房间号）。"""
        occupied = await self._orders.list_room_ids_with_open_stay(on_date=today)
        candidates: list[str] = []
        skipped_occupied: list[str] = []
        skipped_maintenance: list[str] = []
        for room in await self._rooms.list_all():
            if room.status in ROOM_BLOCKED_STATUSES:
                skipped_maintenance.append(room.room_no)
            elif room.id in occupied or room.status == ROOM_IN_HOUSE_STATUS:
                skipped_occupied.append(room.room_no)
            else:
                candidates.append(room.room_no)
        return RotationPlan(
            candidates=tuple(candidates),
            skipped_occupied=tuple(skipped_occupied),
            skipped_maintenance=tuple(skipped_maintenance),
        )

    async def rotate_daily_room(
        self, *, room_no: str, client: LockClient, now: datetime | None = None
    ) -> RoomRotation:
        """空置房每日轮换：到点 next 转正、今日凭证缺失/错位则兜底签发、始终补次日 next。

        边界（酒店本地时区）：今日凭证窗口 [今日 00:00, 明日 00:00)，次日凭证窗口
        [明日 00:00, 后日 00:00)。执行处复验房间存在、非维保、房态非在住且无住宿存续订单
        （房态与订单双口径，任一命中即跳过）——在住与在途客人手里的码不动（续订由
        `extend_for_renewal` 延长窗口）。
        """
        moment = now or datetime.now(HOTEL_TIMEZONE)
        today_start = _day_start(moment.date())
        tomorrow_start = today_start + DAY
        day_after_start = tomorrow_start + DAY

        room = await self._rooms.get_by_room_no(room_no)
        if room is None:
            raise NotFoundError("room", room_no)
        if room.status in ROOM_BLOCKED_STATUSES:
            raise ConflictError(f"房间维保中，跳过每日轮换：{room_no}")
        occupied_by_room_status = room.status == ROOM_IN_HOUSE_STATUS
        occupied_by_order = room.id in await self._orders.list_room_ids_with_open_stay(
            on_date=moment.date()
        )
        if occupied_by_room_status or occupied_by_order:
            raise ConflictError(f"房间有在住/在途订单，跳过每日轮换：{room_no}")

        active = await self._credentials.get_active_for_room(room_no)
        pending = await self._credentials.get_next_for_room(room_no)
        promoted = False

        # 1) 窗口已到点的 next 转正；旧当前凭证（上一日窗口）先作废再转正（单间唯一 active）
        if pending is not None and pending.valid_from <= moment < pending.valid_to:
            await client.activate_next_credential(room_no=room_no, expected_version=pending.version)
            if active is not None and not await self._credentials.revoke(active.id):
                raise ConflictError(f"凭证并发改写，旧凭证吊销未生效：{room_no}")
            if not await self._credentials.activate_next(pending.id):
                raise ConflictError(f"凭证并发改写，next 激活未生效：{room_no}")
            active, pending, promoted = pending, None, True

        # 2) 当前凭证不是今日窗口（缺失、过期或窗口错位）→ 兜底签发今日凭证
        if active is None or active.valid_from != today_start:
            issued = await client.issue_credential(
                room_no=room_no,
                reason="daily",
                valid_from=today_start,
                valid_to=tomorrow_start,
            )
            if active is not None and not await self._credentials.revoke(active.id):
                raise ConflictError(f"凭证并发改写，旧凭证吊销未生效：{room_no}")
            active = await self._create_row(
                room=room,
                issued=issued,
                reason="daily",
                status="active",
                valid_from=today_start,
                valid_to=tomorrow_start,
            )

        # 3) 排定次日 next：窗口错位的先作废（设备与本地同时清），再预生成 [明日, 后日)
        if pending is not None and pending.valid_from != tomorrow_start:
            await _clear_device_next(
                room_no=room_no, expected_version=pending.version, client=client
            )
            if not await self._credentials.revoke(pending.id, from_statuses=("next",)):
                raise ConflictError(f"凭证并发改写，next 作废未生效：{room_no}")
            pending = None
        if pending is None:
            scheduled = await client.schedule_next_credential(
                room_no=room_no, valid_from=tomorrow_start, valid_to=day_after_start
            )
            pending = await self._create_row(
                room=room,
                issued=scheduled,
                reason="daily",
                status="next",
                valid_from=tomorrow_start,
                valid_to=day_after_start,
            )

        return RoomRotation(
            room_no=room_no,
            promoted=promoted,
            version=active.version,
            valid_from=active.valid_from,
            valid_to=active.valid_to,
            next_version=pending.version,
            next_valid_from=pending.valid_from,
            next_valid_to=pending.valid_to,
        )

    async def extend_for_renewal(
        self, *, order: Order, client: LockClient
    ) -> CredentialWindow | None:
        """续订保持当前凭证：只把生效终点延到新退房日期对应窗口，不换码、不换版本。

        订单未分配房间或凭证未就绪（未支付、签发失败、已吊销）时返回 None：续订事实
        仍成立，凭证由既有链路补齐。设备不可达 / 版本冲突抛 LockError 上抛，调用方
        失败关闭——不得出现"订单已续、密码失效"的分叉。
        """
        if order.room_id is None:
            return None
        room = await self._rooms.get_by_id(order.room_id)
        if room is None:
            return None
        current = await self._credentials.get_active_for_room(room.room_no)
        if current is None:
            return None

        valid_to = datetime.now(HOTEL_TIMEZONE) + timedelta(hours=_valid_hours(order))
        if valid_to <= current.valid_to + EXTEND_TOLERANCE:
            return CredentialWindow(
                room_no=room.room_no,
                version=current.version,
                valid_from=current.valid_from,
                valid_to=current.valid_to,
                extended=False,
            )
        await client.extend_credential(
            room_no=room.room_no, valid_to=valid_to, expected_version=current.version
        )
        if not await self._credentials.update_fields(current.id, {"valid_to": valid_to}):
            raise ConflictError(f"凭证并发改写，续订展期未生效：{room.room_no}")
        await self._record_order_event(
            order,
            {
                "type": "credential_extended",
                "room_no": room.room_no,
                "version": current.version,
                "valid_to": valid_to.isoformat(),
            },
        )
        return CredentialWindow(
            room_no=room.room_no,
            version=current.version,
            valid_from=current.valid_from,
            valid_to=valid_to,
            extended=True,
        )

    async def handle_leak(
        self,
        *,
        room_no: str,
        client: LockClient,
        expected_version: int | None = None,
    ) -> CredentialFact:
        """泄露处置：更换当前凭证（reason=leak）并清空 next，明文只随返回值一次性流转。

        保持原凭证窗口（在住客人剩余住宿不受影响；已过期的悬挂凭证按最短窗口重签）。
        落库仍只有 code_hash；调用方展示后即弃，禁止回显与持久化（A2）。
        """
        room = await self._rooms.get_by_room_no(room_no)
        if room is None:
            raise NotFoundError("room", room_no)
        current = await self._credentials.get_active_for_room(room_no)
        if current is None:
            raise NotFoundError("lock_credential", room_no)
        _ensure_expected_version(current, expected_version)

        pending = await self._credentials.get_next_for_room(room_no)
        if pending is not None:
            await _clear_device_next(
                room_no=room_no, expected_version=pending.version, client=client
            )
            if not await self._credentials.revoke(pending.id, from_statuses=("next",)):
                raise ConflictError(f"凭证并发改写，next 作废未生效：{room_no}")

        moment = datetime.now(HOTEL_TIMEZONE)
        valid_from, valid_to = current.valid_from, current.valid_to
        if valid_to <= moment:
            valid_from, valid_to = moment, moment + timedelta(hours=MIN_VALID_HOURS)

        issued = await client.rotate_credential(
            room_no=room_no,
            reason="leak",
            expected_version=current.version,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        if not await self._credentials.revoke(current.id):
            raise ConflictError(f"凭证并发改写，泄露轮换未生效：{room_no}")
        return await self._persist(
            order=None,
            room_id=room.id,
            room_no=room_no,
            floor=room.floor,
            issued=issued,
            reason="leak",
            idempotency_key=None,
        )

    async def list_unlock_records(
        self, *, client: LockClient, room_no: str | None = None
    ) -> list[UnlockRecord]:
        return await client.list_unlock_records(room_no=room_no)

    async def _create_row(
        self,
        *,
        room: Room,
        issued: IssuedCredential,
        reason: str,
        status: str,
        valid_from: datetime,
        valid_to: datetime,
    ) -> LockCredential:
        """设备已签发的事实落库：(room_no, version) 冲突意味着设备与本地已分叉，显式失败。"""
        try:
            return await self._credentials.create(
                room_id=room.id,
                room_no=room.room_no,
                code_hash=hash_lock_code(
                    room_no=room.room_no, version=issued.version, code=issued.code, key_version=1
                ),
                reason=reason,
                version=issued.version,
                valid_from=valid_from,
                valid_to=valid_to,
                status=status,
                device_credential_id=issued.credential_id,
            )
        except IntegrityError as exc:
            raise ConflictError(
                f"凭证版本冲突（设备侧与本地记录可能已分叉，需人工核对）：{room.room_no}"
            ) from exc

    async def _record_order_event(self, order: Order, event: dict[str, Any]) -> None:
        """订单事件只追加：并发未命中只告警，不阻断用例（凭证事实以凭证行为准）。"""
        written = await self._orders.apply_fields(
            order.id, from_statuses=(order.status,), values={}, event=event
        )
        if not written:
            logger.warning("订单事件追加未命中（订单状态并发改写）：order_id=%s", order.id)

    async def _assign_room(self, order: Order) -> Room:
        if order.room_id is not None:
            assigned = await self._rooms.get_by_id(order.room_id)
            if assigned is not None:
                return assigned
        candidates = await self._rooms.list_all(room_type_id=order.room_type_id)
        for room in candidates:
            if room.status in ROOM_BLOCKED_STATUSES:
                continue
            overlaps = await self._orders.count_room_overlaps(
                room_id=room.id,
                check_in_date=order.check_in_date,
                check_out_date=order.check_out_date,
                exclude_order_id=order.id,
            )
            if overlaps == 0:
                if not await self._orders.apply_fields(
                    order.id, from_statuses=(order.status,), values={"room_id": room.id}
                ):
                    raise ConflictError("订单状态并发改写，房间分配未生效")
                return room
        raise ConflictError("该房型暂无可用房间，无法分配并签发凭证")

    async def _persist(
        self,
        *,
        order: Order | None,
        room_id: UUID,
        room_no: str,
        floor: int,
        issued: IssuedCredential,
        reason: str,
        idempotency_key: str | None,
        event_type: str = "credential_issued",
    ) -> CredentialFact:
        try:
            async with self._session.begin_nested():
                row = await self._credentials.create(
                    room_id=room_id,
                    room_no=room_no,
                    code_hash=hash_lock_code(
                        room_no=room_no, version=issued.version, code=issued.code, key_version=1
                    ),
                    reason=reason,
                    version=issued.version,
                    valid_from=issued.valid_from,
                    valid_to=issued.valid_to,
                    status="active",
                    device_credential_id=issued.credential_id,
                    idempotency_key=idempotency_key,
                )
        except IntegrityError:
            if idempotency_key is None:
                raise ConflictError(f"凭证并发签发冲突：{room_no}") from None
            existing = await self._credentials.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise ConflictError(f"凭证并发签发冲突：{room_no}") from None
            return _fact(existing, order_id=order.id if order else None, floor=floor, replayed=True)

        if order is not None:
            await self._orders.apply_fields(
                order.id,
                from_statuses=(order.status,),
                values={},
                event={
                    "type": event_type,
                    "room_no": room_no,
                    "version": issued.version,
                    "reason": reason,
                },
            )
        return _fact(
            row,
            order_id=order.id if order else None,
            floor=floor,
            replayed=False,
            code=issued.code,
        )


def _fact(
    credential: LockCredential,
    *,
    order_id: UUID | None,
    floor: int,
    replayed: bool,
    code: str | None = None,
) -> CredentialFact:
    return CredentialFact(
        order_id=order_id,
        room_id=credential.room_id,
        room_no=credential.room_no,
        floor=floor,
        credential_id=credential.id,
        device_credential_id=credential.device_credential_id,
        version=credential.version,
        reason=credential.reason,
        valid_from=credential.valid_from,
        valid_to=credential.valid_to,
        replayed=replayed,
        code=code,
    )


def _ensure_expected_version(current: LockCredential, expected_version: int | None) -> None:
    if expected_version is not None and current.version != expected_version:
        raise ConflictError(
            f"凭证版本不匹配：期望 {expected_version}，当前 {current.version}（拒绝重放改密）"
        )


def _day_start(day: date) -> datetime:
    """酒店本地时区的当日零点：每日轮换窗口边界的唯一口径。"""
    return datetime.combine(day, time.min, tzinfo=HOTEL_TIMEZONE)


async def _clear_device_next(*, room_no: str, expected_version: int, client: LockClient) -> None:
    """设备侧清空 next：槽位已空（404）视为达成，其余错误上抛。"""
    try:
        await client.revoke_next_credential(room_no=room_no, expected_version=expected_version)
    except LockError as exc:
        if exc.status_code != 404:
            raise
        logger.warning("设备侧 next 槽位已空，继续清理本地记录：room_no=%s", room_no)


def _valid_hours(order: Order) -> int:
    days = (order.check_out_date - datetime.now(HOTEL_TIMEZONE).date()).days
    return max(MIN_VALID_HOURS, days * 24 + MIN_VALID_HOURS)
