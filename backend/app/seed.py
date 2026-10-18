"""种子数据：幂等补齐房型/房间/员工/账号与演示单据，供本地联调与验收复现。

运行：`python -m app.seed`（可重复执行：按自然唯一键判存，不覆盖既有数据）。
账号口令见 README「本地开发（后端）」；仅用于开发环境，生产必须重新签发。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import SessionFactory
from app.models.data_pipeline import DataCleaningRule
from app.models.domain import Room
from app.models.mcp_server import McpServer
from app.repositories import (
    GuestRepository,
    HousekeepingTaskRepository,
    LockCredentialRepository,
    OrderRepository,
    RoomRepository,
    RoomTypeRepository,
    StaffRepository,
    UserRepository,
)
from app.security import hash_password
from app.services.order_service import OrderService

logger = logging.getLogger(__name__)

DEV_PASSWORDS = {
    "admin-demo": "admin-dev-pass",
    "manager-demo": "manager-dev-pass",
    "operator-demo": "operator-dev-pass",
    "staff-2001": "staff-dev-pass",
    "staff-2002": "staff-dev-pass",
    "guest-1001": "guest-dev-pass",
}


@dataclass(frozen=True)
class RoomTypeSpec:
    code: str
    name: str
    base_price_fen: int
    capacity: int
    bed_type: str
    amenities: tuple[str, ...]


ROOM_TYPES: tuple[RoomTypeSpec, ...] = (
    RoomTypeSpec("KING", "皓晨大床房", 39800, 2, "king", ("wifi", "智能门锁", "投影")),
    RoomTypeSpec("TWIN", "皓晨双床房", 45800, 2, "twin", ("wifi", "智能门锁", "投影")),
    RoomTypeSpec(
        "FAMILY", "皓晨家庭套房", 69800, 4, "king+twin", ("wifi", "智能门锁", "投影", "儿童用品")
    ),
    RoomTypeSpec(
        "EXEC",
        "皓晨行政房",
        59800,
        2,
        "king",
        ("wifi", "智能门锁", "投影", "迷你吧", "浴缸", "吹风机"),
    ),
    RoomTypeSpec(
        "SUITE",
        "皓晨总统套房",
        128800,
        4,
        "king+twin",
        ("wifi", "智能门锁", "投影", "迷你吧", "浴缸", "沙发", "阳台", "保险箱"),
    ),
)

ROOM_PLAN: tuple[tuple[str, str, int], ...] = (
    ("301", "KING", 3),
    ("302", "KING", 3),
    ("303", "KING", 3),
    ("304", "KING", 3),
    ("401", "TWIN", 4),
    ("402", "TWIN", 4),
    ("403", "TWIN", 4),
    ("404", "TWIN", 4),
    ("501", "FAMILY", 5),
    ("502", "FAMILY", 5),
    ("503", "FAMILY", 5),
    ("504", "FAMILY", 5),
    ("601", "EXEC", 6),
    ("602", "EXEC", 6),
    ("603", "EXEC", 6),
    ("604", "EXEC", 6),
    ("701", "SUITE", 7),
    ("702", "SUITE", 7),
)

DEMO_ORDER_NO = "DEMO-0001"
DEMO_ROOM_NO = "301"
DEMO_ORDER_AMOUNT_FEN = 119400
DEMO_CREDENTIAL_VALID_DAYS = 2
DEMO_TASK_DEDUPE_KEY = "seed:call_clean:302"

# MCP 网关各渠道端点（子路径挂载）：URL 前缀取 settings.mcp_gateway_base_url。
MCP_GATEWAY_SERVERS: tuple[dict[str, object], ...] = (
    {
        "slug": "lock-mcp",
        "name": "门锁 MCP 网关",
        "path": "/lock/mcp",
        "description": "lock-mcp：凭证签发/轮换/吊销/开锁记录（内部走 device-sim 契约模拟器）",
        "read_only_tools": ["get_credential_metadata", "list_unlock_records"],
    },
    {
        "slug": "robot-mcp",
        "name": "机器人 MCP 网关",
        "path": "/robot/mcp",
        "description": "robot-mcp：机器人配送下单与状态查询（内部走 device-sim 契约模拟器）",
        "read_only_tools": ["get_delivery"],
    },
    {
        "slug": "wechat-mcp",
        "name": "微信 MCP 网关",
        "path": "/wechat/mcp",
        "description": "wechat-mcp：支付单状态查询（不返回 openid/prepay_id）",
        "read_only_tools": ["get_pay_order_status"],
    },
)

# 数据管道默认规则：9 个 handler 各一条，带生产合理 cron 与参数（可编辑、可停用）。
# 导出入湖类与预聚合相位错开：export 每 30 分钟，aggregate 每小时 10 分，避免空跑。
CLEANING_RULE_PLAN: tuple[dict[str, object], ...] = (
    {
        "name": "会话数据脱敏",
        "task_type": "data_pipeline.clean_conversations",
        "schedule_cron": "0 3 * * *",
        "config": {"batch_size": 500, "dry_run": False},
        "description": "扫描 messages，对手机号/身份证号脱敏",
    },
    {
        "name": "反馈数据去重",
        "task_type": "data_pipeline.dedup_feedback",
        "schedule_cron": "0 4 * * *",
        "config": {"dry_run": False},
        "description": "按 (message_id, user_id) 合并重复反馈，保留最新",
    },
    {
        "name": "审计记录归档",
        "task_type": "data_pipeline.archive_audit_logs",
        "schedule_cron": "0 5 * * *",
        "config": {"retention_days": 90, "tables": ["model_audit", "tool_audit"]},
        "description": "将 90 天前审计记录迁至 data_archives 并软删",
    },
    {
        "name": "知识库一致性校验",
        "task_type": "data_pipeline.validate_kb_consistency",
        "schedule_cron": "0 6 * * 1",
        "config": {"freshness_days": 180, "min_chunk_length": 10},
        "description": "检测过期文档、空块与重复块",
    },
    {
        "name": "质检标签清洗",
        "task_type": "data_pipeline.clean_qa_labels",
        "schedule_cron": "0 7 * * *",
        "config": {"confidence_threshold": 0.6, "dry_run": False},
        "description": "低置信度自动标签标记待人工复核",
    },
    {
        "name": "数据湖-消息导出",
        "task_type": "data_pipeline.export_messages",
        "schedule_cron": "*/30 * * * *",
        "config": {"batch_size": 2000},
        "description": "messages 增量导出至数据湖 Parquet",
    },
    {
        "name": "数据湖-审计导出",
        "task_type": "data_pipeline.export_audits",
        "schedule_cron": "*/30 * * * *",
        "config": {"batch_size": 2000},
        "description": "model_audit / tool_audit 增量导出",
    },
    {
        "name": "数据湖-订单导出",
        "task_type": "data_pipeline.export_business",
        "schedule_cron": "*/30 * * * *",
        "config": {"batch_size": 2000},
        "description": "订单事实 + 房型/房间维度导出",
    },
    {
        "name": "数据湖-预聚合",
        "task_type": "data_pipeline.build_aggregates",
        "schedule_cron": "10 * * * *",
        "config": {},
        "description": "DuckDB 生成 token/工具/房型月度预聚合",
    },
)

ACCOUNT_PLAN: tuple[tuple[str, str, str], ...] = (
    ("admin-demo", "admin", "皓晨管理员"),
    ("manager-demo", "manager", "皓晨店长"),
    ("operator-demo", "operator", "皓晨客服"),
    ("staff-2001", "staff", "王芳"),
    ("staff-2002", "staff", "李静"),
    ("guest-1001", "customer", "演示客人"),
)

STAFF_PLAN: tuple[tuple[str, str, str], ...] = (
    ("HK-2001", "王芳", "staff-2001"),
    ("HK-2002", "李静", "staff-2002"),
)


@dataclass
class SeedReport:
    created: dict[str, int] = field(default_factory=dict)

    def count(self, kind: str) -> None:
        self.created[kind] = self.created.get(kind, 0) + 1


def demo_code_hash(room_no: str, version: int) -> str:
    """演示凭证的 code_hash：仓库不保存任何明文门锁密码（A2）。"""
    return hashlib.sha256(f"demo-credential:{room_no}:{version}".encode()).hexdigest()


async def _seed_accounts(session: AsyncSession, report: SeedReport) -> dict[str, UUID]:
    users = UserRepository(session)
    ids: dict[str, UUID] = {}
    for username, role, display_name in ACCOUNT_PLAN:
        user = await users.get_by_username(username)
        if user is None:
            user = await users.create_account(
                username=username,
                password_hash=hash_password(DEV_PASSWORDS[username]),
                role=role,
                display_name=display_name,
            )
            report.count("accounts")
        ids[username] = user.id
    return ids


async def _seed_room_types(session: AsyncSession, report: SeedReport) -> dict[str, UUID]:
    repo = RoomTypeRepository(session)
    ids: dict[str, UUID] = {}
    for spec in ROOM_TYPES:
        row = await repo.get_by_code(spec.code)
        if row is None:
            row = await repo.create(
                code=spec.code,
                name=spec.name,
                base_price_fen=spec.base_price_fen,
                capacity=spec.capacity,
                bed_type=spec.bed_type,
                amenities=list(spec.amenities),
            )
            report.count("room_types")
        ids[spec.code] = row.id
    return ids


async def _seed_rooms(
    session: AsyncSession, report: SeedReport, room_type_ids: dict[str, UUID]
) -> dict[str, Room]:
    repo = RoomRepository(session)
    rooms: dict[str, Room] = {}
    for room_no, type_code, floor in ROOM_PLAN:
        row = await repo.get_by_room_no(room_no)
        if row is None:
            row = await repo.create(
                room_no=room_no,
                room_type_id=room_type_ids[type_code],
                floor=floor,
                lock_device_no=f"LOCK-{room_no}",
            )
            report.count("rooms")
        rooms[room_no] = row
    return rooms


async def _seed_staff(
    session: AsyncSession, report: SeedReport, account_ids: dict[str, UUID]
) -> dict[str, UUID]:
    repo = StaffRepository(session)
    ids: dict[str, UUID] = {}
    for staff_no, name, username in STAFF_PLAN:
        row = await repo.get_by_staff_no(staff_no)
        if row is None:
            row = await repo.create(staff_no=staff_no, name=name, user_id=account_ids[username])
            report.count("staff")
        ids[staff_no] = row.id
    return ids


async def _seed_demo_flow(
    session: AsyncSession,
    report: SeedReport,
    *,
    rooms: dict[str, Room],
    staff_ids: dict[str, UUID],
    account_ids: dict[str, UUID],
) -> None:
    """演示主链路：客人入住中订单 + 房间有效凭证 + 一条待接单的呼叫清洁。"""
    now = datetime.now(UTC)
    guests = GuestRepository(session)
    guest = await guests.get_by_user_id(account_ids["guest-1001"])
    if guest is None:
        guest = await guests.create(user_id=account_ids["guest-1001"], nickname="演示客人")
        report.count("guests")

    room = rooms[DEMO_ROOM_NO]
    orders = OrderRepository(session)
    order = await orders.get_by_order_no(DEMO_ORDER_NO)
    if order is None:
        order_service = OrderService(session)
        order = await order_service.create(
            order_no=DEMO_ORDER_NO,
            guest_id=guest.id,
            room_type_id=room.room_type_id,
            room_id=room.id,
            check_in_date=(now - timedelta(days=1)).date(),
            check_out_date=(now + timedelta(days=2)).date(),
            amount_fen=DEMO_ORDER_AMOUNT_FEN,
        )
        await order_service.transition(order.id, "pending_payment")
        await order_service.transition(
            order.id, "paid", extra={"paid_amount_fen": DEMO_ORDER_AMOUNT_FEN}
        )
        await order_service.transition(order.id, "checked_in")
        await RoomRepository(session).update_fields(room.id, {"status": "occupied"})
        report.count("orders")

    credentials = LockCredentialRepository(session)
    if await credentials.get_active_for_room(DEMO_ROOM_NO) is None:
        version = await credentials.latest_version(DEMO_ROOM_NO) + 1
        await credentials.create(
            room_id=room.id,
            room_no=DEMO_ROOM_NO,
            code_hash=demo_code_hash(DEMO_ROOM_NO, version),
            reason="checkin",
            version=version,
            valid_from=now - timedelta(days=1),
            valid_to=now + timedelta(days=DEMO_CREDENTIAL_VALID_DAYS),
            device_credential_id=f"DEMO-CRED-{DEMO_ROOM_NO}",
        )
        report.count("lock_credentials")

    tasks = HousekeepingTaskRepository(session)
    if await tasks.get_by_idempotency_key(DEMO_TASK_DEDUPE_KEY) is None:
        await tasks.create(
            task_type="call_clean",
            room_id=rooms["302"].id,
            room_no="302",
            assigned_staff_id=staff_ids["HK-2001"],
            payload={"scene": "演示派单：客人呼叫客房清洁"},
            due_at=now + timedelta(hours=4),
            idempotency_key=DEMO_TASK_DEDUPE_KEY,
        )
        report.count("housekeeping_tasks")


async def _seed_cleaning_rules(session: AsyncSession, report: SeedReport) -> None:
    """幂等补齐数据管道默认规则：按 task_type 判存，不覆盖既有。"""
    existing = set((await session.execute(select(DataCleaningRule.task_type))).scalars().all())
    for spec in CLEANING_RULE_PLAN:
        task_type = str(spec["task_type"])
        if task_type in existing:
            continue
        config_raw = spec.get("config") or {}
        config_val: dict[str, object] = dict(config_raw) if isinstance(config_raw, dict) else {}
        session.add(
            DataCleaningRule(
                name=str(spec["name"]),
                description=str(spec.get("description") or ""),
                task_type=task_type,
                handler_version=1,
                schedule_cron=str(spec.get("schedule_cron") or "") or None,
                config=config_val,
                enabled=True,
            )
        )
        report.count("cleaning_rules")


async def _seed_mcp_servers(session: AsyncSession, report: SeedReport) -> None:
    """幂等注册 mcp-gateway 的三个渠道 server；已存在的 slug 不覆盖（保留管理端改动）。"""
    base_url = get_settings().mcp_gateway_base_url.rstrip("/")
    existing = set((await session.execute(select(McpServer.slug))).scalars().all())
    for item in MCP_GATEWAY_SERVERS:
        slug = str(item["slug"])
        if slug in existing:
            continue
        session.add(
            McpServer(
                slug=slug,
                name=str(item["name"]),
                description=str(item["description"]),
                server_url=f"{base_url}{item['path']}",
                transport="streamable_http",
                config_json={
                    "timeout_seconds": 10,
                    "read_only_tools": cast(list[str], item["read_only_tools"]),
                },
                active=True,
            )
        )
        report.count("mcp_servers")


async def seed(session: AsyncSession) -> SeedReport:
    report = SeedReport()
    account_ids = await _seed_accounts(session, report)
    room_type_ids = await _seed_room_types(session, report)
    rooms = await _seed_rooms(session, report, room_type_ids)
    staff_ids = await _seed_staff(session, report, account_ids)
    await _seed_demo_flow(
        session, report, rooms=rooms, staff_ids=staff_ids, account_ids=account_ids
    )
    await _seed_cleaning_rules(session, report)
    await _seed_mcp_servers(session, report)
    await session.commit()
    return report


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    async with SessionFactory() as session:
        report = await seed(session)
    logger.info("种子数据完成：%s", report.created or "无新增（幂等命中）")
    logger.info("开发账号：%s", ", ".join(sorted(DEV_PASSWORDS)))


if __name__ == "__main__":
    asyncio.run(main())
