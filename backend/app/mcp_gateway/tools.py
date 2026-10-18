"""MCP 工具定义：把既有渠道客户端包装为 MCP 工具（协议转换，不改业务语义）。

每个工具函数调用既有 `LockClient` / `RobotClient` / `WechatClient`；工具 `name` 不含
server 前缀（前缀由 Agent 侧装配时按 server slug 拼接，见 `app/agents/mcp/runtime.py`）。
渠道明确失败与不可达都收敛为 `{ok: false, error}` 结构化结果，不向 Agent 抛传输异常。
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.lock.client import LockClient, LockError, LockUnavailableError
from app.robot.client import RobotClient, RobotError, RobotUnavailableError
from app.wechat.client import WechatClient, WechatError, WechatUnavailableError

logger = logging.getLogger(__name__)

LOCK_SERVER_NAME = "lock-mcp"
ROBOT_SERVER_NAME = "robot-mcp"
WECHAT_SERVER_NAME = "wechat-mcp"


def build_lock_server(client: LockClient | None = None) -> FastMCP:
    """构造 lock-mcp：凭证生成/轮换/吊销/查询/开锁记录。"""
    server = FastMCP(
        LOCK_SERVER_NAME,
        instructions="无人酒店门锁渠道：凭证签发、轮换、吊销与开锁记录查询。",
        host="0.0.0.0",
        port=9200,
        streamable_http_path="/mcp",
    )
    lock = client or LockClient.from_settings()

    @server.tool(name="issue_credential", description="为房间签发一张有效门锁凭证（返回明文码）")
    async def issue_credential(room_no: str, valid_hours: int | None = None) -> dict[str, Any]:
        try:
            issued = await lock.issue_credential(room_no=room_no, valid_hours=valid_hours)
        except LockUnavailableError as exc:
            return {"ok": False, "error": f"门锁服务不可达：{exc}"}
        except LockError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "credential_id": issued.credential_id,
            "room_no": issued.room_no,
            "code": issued.code,
            "version": issued.version,
            "valid_from": issued.valid_from.isoformat(),
            "valid_to": issued.valid_to.isoformat(),
        }

    @server.tool(name="rotate_credential", description="轮换房间门锁凭证（旧码作废）")
    async def rotate_credential(
        room_no: str, expected_version: int | None = None, valid_hours: int | None = None
    ) -> dict[str, Any]:
        try:
            issued = await lock.rotate_credential(
                room_no=room_no, expected_version=expected_version, valid_hours=valid_hours
            )
        except LockUnavailableError as exc:
            return {"ok": False, "error": f"门锁服务不可达：{exc}"}
        except LockError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "room_no": issued.room_no,
            "code": issued.code,
            "version": issued.version,
        }

    @server.tool(name="revoke_credential", description="吊销房间门锁凭证")
    async def revoke_credential(
        room_no: str, expected_version: int | None = None
    ) -> dict[str, Any]:
        try:
            meta = await lock.revoke_credential(room_no=room_no, expected_version=expected_version)
        except LockUnavailableError as exc:
            return {"ok": False, "error": f"门锁服务不可达：{exc}"}
        except LockError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "room_no": meta.room_no, "status": meta.status}

    @server.tool(
        name="get_credential_metadata",
        description="查询房间当前凭证元数据（只读，不含明文码）",
    )
    async def get_credential_metadata(room_no: str) -> dict[str, Any]:
        try:
            meta = await lock.get_credential(room_no)
        except LockUnavailableError as exc:
            return {"ok": False, "error": f"门锁服务不可达：{exc}"}
        except LockError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "room_no": meta.room_no,
            "version": meta.version,
            "status": meta.status,
            "valid_from": meta.valid_from.isoformat(),
            "valid_to": meta.valid_to.isoformat(),
        }

    @server.tool(name="list_unlock_records", description="查询开锁记录（只读）")
    async def list_unlock_records(room_no: str | None = None) -> dict[str, Any]:
        try:
            records = await lock.list_unlock_records(room_no=room_no)
        except LockUnavailableError as exc:
            return {"ok": False, "error": f"门锁服务不可达：{exc}"}
        except LockError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "records": [
                {
                    "record_id": r.record_id,
                    "room_no": r.room_no,
                    "source": r.source,
                    "accepted": r.accepted,
                    "at": r.at.isoformat(),
                }
                for r in records
            ],
        }

    return server


def build_robot_server(client: RobotClient | None = None) -> FastMCP:
    """构造 robot-mcp：配送下单与状态查询。"""
    server = FastMCP(
        ROBOT_SERVER_NAME,
        instructions="酒店机器人配送渠道：创建配送单、查询配送状态。",
        host="0.0.0.0",
        port=9200,
        streamable_http_path="/mcp",
    )
    robot = client or RobotClient.from_settings()

    @server.tool(name="create_delivery", description="为房间创建一张机器人配送单")
    async def create_delivery(order_no: str, room_no: str, item: str) -> dict[str, Any]:
        try:
            delivery = await robot.create_delivery(order_no=order_no, room_no=room_no, item=item)
        except RobotUnavailableError as exc:
            return {"ok": False, "error": f"机器人渠道不可达：{exc}"}
        except RobotError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "delivery_id": delivery.delivery_id,
            "order_no": delivery.order_no,
            "room_no": delivery.room_no,
            "item": delivery.item,
            "status": delivery.status,
        }

    @server.tool(
        name="get_delivery",
        description="查询配送单状态（只读）",
    )
    async def get_delivery(delivery_id: str) -> dict[str, Any]:
        try:
            delivery = await robot.get_delivery(delivery_id)
        except RobotUnavailableError as exc:
            return {"ok": False, "error": f"机器人渠道不可达：{exc}"}
        except RobotError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "delivery_id": delivery.delivery_id,
            "order_no": delivery.order_no,
            "room_no": delivery.room_no,
            "item": delivery.item,
            "status": delivery.status,
            "updated_at": delivery.updated_at.isoformat(),
        }

    return server


def build_wechat_server(client: WechatClient | None = None) -> FastMCP:
    """构造 wechat-mcp：支付单状态查询（不含登录凭证与支付参数）。"""
    server = FastMCP(
        WECHAT_SERVER_NAME,
        instructions="微信渠道：查询支付单状态（只读，不返回 openid/prepay_id）。",
        host="0.0.0.0",
        port=9200,
        streamable_http_path="/mcp",
    )
    wechat = client or WechatClient.from_settings()

    @server.tool(
        name="get_pay_order_status",
        description="查询支付单状态（只读）",
    )
    async def get_pay_order_status(order_no: str) -> dict[str, Any]:
        try:
            order = await wechat.get_pay_order(order_no)
        except WechatUnavailableError as exc:
            return {"ok": False, "error": f"微信渠道不可达：{exc}"}
        except WechatError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "order_no": order.order_no,
            "amount_fen": order.amount_fen,
            "status": order.status,
            "notifications": order.notifications,
        }

    return server


__all__ = [
    "LOCK_SERVER_NAME",
    "ROBOT_SERVER_NAME",
    "WECHAT_SERVER_NAME",
    "build_lock_server",
    "build_robot_server",
    "build_wechat_server",
]
