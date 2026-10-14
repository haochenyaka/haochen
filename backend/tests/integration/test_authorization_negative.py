"""越权负向集成测试（真实 HTTP + 真实 PostgreSQL）：A6「服务端授权」取证入口。

覆盖 development-plan P2 退出标准的负例：跨员工任务、跨房间凭证、非管理员配置修改，
以及未认证 / 伪造令牌一律拒绝；每个负例同时断言数据未被改写。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.domain import HousekeepingTask, LockCredential, Room, RoomType
from app.repositories import UserRepository
from app.repositories.guest_repository import GuestRepository
from app.repositories.housekeeping_repository import HousekeepingTaskRepository
from app.repositories.lock_credential_repository import LockCredentialRepository
from app.repositories.room_repository import RoomRepository, RoomTypeRepository
from app.repositories.staff_repository import StaffRepository
from app.security import hash_password
from app.services.order_service import OrderService

pytestmark = pytest.mark.integration

PASSWORD = "authz-pass-1"


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


@dataclass
class World:
    staff_a_token: str
    staff_b_token: str
    guest_a_token: str
    guest_b_token: str
    manager_token: str
    staff_a_id: UUID
    task_a_id: UUID
    task_b_id: UUID
    credential_id: UUID
    credential_b_id: UUID
    room_a_row_id: UUID
    room_b_row_id: UUID
    order_id: UUID
    room_a: str
    room_b: str
    credential_hash: str
    room_type_code: str
    snapshots: dict[str, object] = field(default_factory=dict)


@pytest.fixture
def api(runtime_db_url: str) -> Iterator[Api]:
    engine = create_async_engine(runtime_db_url, poolclass=NullPool)

    async def _override_session() -> AsyncSession:  # type: ignore[misc]
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    try:
        with TestClient(app) as client:
            yield Api(client=client, engine=engine)
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())


def _login(api: Api, *, username: str) -> str:
    response = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _build_world(api: Api) -> World:
    suffix = uuid4().hex[:8]
    room_a = f"A-{suffix}"
    room_b = f"B-{suffix}"
    room_type_code = f"RT-{suffix}"

    async def _body() -> dict[str, object]:
        now = datetime.now(UTC)
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            users = UserRepository(session)

            async def _account(username: str, role: str) -> UUID:
                user = await users.create_account(
                    username=username,
                    password_hash=hash_password(PASSWORD),
                    role=role,
                    display_name=f"{role}-{username}",
                )
                return user.id

            staff_a = await StaffRepository(session).create(
                staff_no=f"HK-A-{suffix}",
                name="保洁甲",
                user_id=await _account(f"hk-a-{suffix}", "staff"),
            )
            staff_b = await StaffRepository(session).create(
                staff_no=f"HK-B-{suffix}",
                name="保洁乙",
                user_id=await _account(f"hk-b-{suffix}", "staff"),
            )
            guests = GuestRepository(session)
            guest_a = await guests.create(
                user_id=await _account(f"guest-a-{suffix}", "customer"), nickname="客人甲"
            )
            await guests.create(
                user_id=await _account(f"guest-b-{suffix}", "customer"), nickname="客人乙"
            )
            await _account(f"manager-{suffix}", "manager")

            room_type = await RoomTypeRepository(session).create(
                code=room_type_code,
                name="验收房型",
                base_price_fen=39800,
                capacity=2,
                bed_type="king",
            )
            rooms = RoomRepository(session)
            room_a_row = await rooms.create(
                room_no=room_a, room_type_id=room_type.id, floor=9, lock_device_no=f"LOCK-{room_a}"
            )
            room_b_row = await rooms.create(
                room_no=room_b, room_type_id=room_type.id, floor=9, lock_device_no=f"LOCK-{room_b}"
            )

            orders = OrderService(session)
            order = await orders.create(
                order_no=f"NO-{suffix}A",
                guest_id=guest_a.id,
                room_type_id=room_type.id,
                room_id=room_a_row.id,
                check_in_date=(now - timedelta(days=1)).date(),
                check_out_date=(now + timedelta(days=1)).date(),
                amount_fen=79600,
            )
            await orders.transition(order.id, "pending_payment")
            await orders.transition(order.id, "paid", extra={"paid_amount_fen": 79600})
            await orders.transition(order.id, "checked_in")

            credentials = LockCredentialRepository(session)
            credential_hash = hashlib.sha256(f"authz:{room_a}:{suffix}".encode()).hexdigest()
            credential = await credentials.create(
                room_id=room_a_row.id,
                room_no=room_a,
                code_hash=credential_hash,
                reason="checkin",
                version=1,
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=2),
                device_credential_id=f"DEV-{room_a}",
            )
            credential_b = await credentials.create(
                room_id=room_b_row.id,
                room_no=room_b,
                code_hash=hashlib.sha256(f"authz:{room_b}:{suffix}".encode()).hexdigest(),
                reason="checkin",
                version=1,
                valid_from=now - timedelta(days=1),
                valid_to=now + timedelta(days=2),
                device_credential_id=f"DEV-{room_b}",
            )

            tasks = HousekeepingTaskRepository(session)
            task_a = await tasks.create(
                task_type="call_clean",
                room_id=room_a_row.id,
                room_no=room_a,
                order_id=order.id,
                assigned_staff_id=staff_a.id,
                payload={"scene": "验收：员工甲的任务"},
            )
            task_b = await tasks.create(
                task_type="call_clean",
                room_id=room_b_row.id,
                room_no=room_b,
                assigned_staff_id=staff_b.id,
                payload={"scene": "验收：员工乙的任务"},
            )
            await session.commit()
            return {
                "staff_a_id": staff_a.id,
                "task_a_id": task_a.id,
                "task_b_id": task_b.id,
                "credential_id": credential.id,
                "credential_b_id": credential_b.id,
                "room_a_row_id": room_a_row.id,
                "room_b_row_id": room_b_row.id,
                "order_id": order.id,
                "credential_hash": credential_hash,
            }

    data = asyncio.run(_body())
    world = World(
        staff_a_token=_login(api, username=f"hk-a-{suffix}"),
        staff_b_token=_login(api, username=f"hk-b-{suffix}"),
        guest_a_token=_login(api, username=f"guest-a-{suffix}"),
        guest_b_token=_login(api, username=f"guest-b-{suffix}"),
        manager_token=_login(api, username=f"manager-{suffix}"),
        staff_a_id=data["staff_a_id"],  # type: ignore[arg-type]
        task_a_id=data["task_a_id"],  # type: ignore[arg-type]
        task_b_id=data["task_b_id"],  # type: ignore[arg-type]
        credential_id=data["credential_id"],  # type: ignore[arg-type]
        credential_b_id=data["credential_b_id"],  # type: ignore[arg-type]
        room_a_row_id=data["room_a_row_id"],  # type: ignore[arg-type]
        room_b_row_id=data["room_b_row_id"],  # type: ignore[arg-type]
        order_id=data["order_id"],  # type: ignore[arg-type]
        room_a=room_a,
        room_b=room_b,
        credential_hash=str(data["credential_hash"]),
        room_type_code=room_type_code,
    )
    world.snapshots = _snapshot(api, world)
    return world


def _snapshot(api: Api, world: World) -> dict[str, object]:
    """负例前后快照：越权请求不得改写任务、凭证、房态与配置表。"""

    async def _body() -> dict[str, object]:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            task_a = await session.get(HousekeepingTask, world.task_a_id)
            task_b = await session.get(HousekeepingTask, world.task_b_id)
            credential = await session.get(LockCredential, world.credential_id)
            credential_b = await session.get(LockCredential, world.credential_b_id)
            room_a = await session.get(Room, world.room_a_row_id)
            room_b = await session.get(Room, world.room_b_row_id)

            async def _count(model: type, *conditions: object) -> int:
                stmt = select(func.count()).select_from(model)
                for condition in conditions:
                    stmt = stmt.where(condition)
                return int((await session.execute(stmt)).scalar_one())

            assert task_a and task_b and credential and credential_b and room_a and room_b
            return {
                "task_a": (task_a.status, task_a.updated_at, task_a.assigned_staff_id),
                "task_b": (task_b.status, task_b.updated_at, task_b.assigned_staff_id),
                "credential": (credential.status, credential.version, credential.revoked_at),
                "credential_b": (credential_b.status, credential_b.version),
                "room_a": (room_a.status, room_a.updated_at),
                "room_b": (room_b.status, room_b.updated_at),
                "room_type_rows": await _count(RoomType, RoomType.code == world.room_type_code),
                "credential_rows": await _count(LockCredential),
            }

    return asyncio.run(_body())


def test_staff_task_reads_are_scoped_to_own_profile(api: Api) -> None:
    world = _build_world(api)

    mine = api.client.get("/api/staff/tasks", headers=_auth(world.staff_a_token))
    assert mine.status_code == 200
    entries = mine.json()
    assert str(world.task_a_id) in [entry["id"] for entry in entries]
    assert all(entry["assigned_staff_id"] == str(world.staff_a_id) for entry in entries)

    own = api.client.get(f"/api/staff/tasks/{world.task_a_id}", headers=_auth(world.staff_a_token))
    assert own.status_code == 200
    assert own.json()["room_no"] == world.room_a

    peer = api.client.get(f"/api/staff/tasks/{world.task_b_id}", headers=_auth(world.staff_a_token))
    assert peer.status_code == 403

    control = api.client.get(
        f"/api/staff/tasks/{world.task_b_id}", headers=_auth(world.staff_b_token)
    )
    assert control.status_code == 200
    assert control.json()["room_no"] == world.room_b

    assert _snapshot(api, world) == world.snapshots


def test_staff_task_endpoints_reject_anonymous_forged_and_wrong_roles(api: Api) -> None:
    world = _build_world(api)

    assert api.client.get("/api/staff/tasks").status_code == 401
    assert (
        api.client.get(
            f"/api/staff/tasks/{world.task_a_id}",
            headers={"Authorization": "Bearer forged-token"},
        ).status_code
        == 401
    )
    assert api.client.get("/api/staff/tasks", headers=_auth(world.guest_a_token)).status_code == 403
    assert api.client.get("/api/staff/tasks", headers=_auth(world.manager_token)).status_code == 403

    assert _snapshot(api, world) == world.snapshots


def test_guest_credential_returns_metadata_only_for_own_room(api: Api) -> None:
    world = _build_world(api)

    response = api.client.get(
        f"/api/guest/rooms/{world.room_a}/credential", headers=_auth(world.guest_a_token)
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["room_no"] == world.room_a
    assert payload["status"] == "active"
    assert payload["reason"] == "checkin"
    assert payload["version"] == 1
    assert payload["valid_from"] and payload["valid_to"]
    assert "code_hash" not in payload
    assert "device_credential_id" not in payload
    assert world.credential_hash not in response.text


def test_guest_credential_denies_other_rooms_and_accounts(api: Api) -> None:
    world = _build_world(api)

    other_guest = api.client.get(
        f"/api/guest/rooms/{world.room_a}/credential", headers=_auth(world.guest_b_token)
    )
    assert other_guest.status_code == 403

    not_my_order = api.client.get(
        f"/api/guest/rooms/{world.room_b}/credential", headers=_auth(world.guest_a_token)
    )
    assert not_my_order.status_code == 403

    unknown_room = api.client.get(
        f"/api/guest/rooms/ghost-{uuid4().hex[:6]}/credential", headers=_auth(world.guest_a_token)
    )
    assert unknown_room.status_code == 403

    staff_account = api.client.get(
        f"/api/guest/rooms/{world.room_a}/credential", headers=_auth(world.staff_a_token)
    )
    assert staff_account.status_code == 403

    assert api.client.get(f"/api/guest/rooms/{world.room_a}/credential").status_code == 401
    assert (
        api.client.get(
            f"/api/guest/rooms/{world.room_a}/credential",
            headers={"Authorization": "Bearer forged-token"},
        ).status_code
        == 401
    )

    assert _snapshot(api, world) == world.snapshots


def test_guest_credential_hidden_after_order_completed(api: Api) -> None:
    world = _build_world(api)

    async def _complete_order() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            orders = OrderService(session)
            await orders.transition(world.order_id, "checkout_pending")
            await orders.transition(world.order_id, "completed")
            await session.commit()

    asyncio.run(_complete_order())
    path = f"/api/guest/rooms/{world.room_a}/credential"
    assert api.client.get(path, headers=_auth(world.guest_a_token)).status_code == 403


def test_guest_credential_missing_when_revoked(api: Api) -> None:
    world = _build_world(api)

    async def _revoke() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await LockCredentialRepository(session).revoke(world.credential_id)
            await session.commit()

    asyncio.run(_revoke())
    path = f"/api/guest/rooms/{world.room_a}/credential"
    assert api.client.get(path, headers=_auth(world.guest_a_token)).status_code == 404


def test_non_admin_cannot_modify_configuration(api: Api) -> None:
    world = _build_world(api)
    body = {
        "code": f"RT-NEW-{uuid4().hex[:6]}",
        "name": "越权新建房型",
        "base_price_fen": 10000,
        "capacity": 2,
        "bed_type": "king",
    }

    assert api.client.post("/api/admin/room-types", json=body).status_code == 401
    assert (
        api.client.post(
            "/api/admin/room-types", json=body, headers={"Authorization": "Bearer forged-token"}
        ).status_code
        == 401
    )
    for token in (world.manager_token, world.staff_a_token, world.guest_a_token):
        denied = api.client.post("/api/admin/room-types", json=body, headers=_auth(token))
        assert denied.status_code == 403

    rename = api.client.patch(
        f"/api/admin/room-types/{uuid4()}",
        json={"name": "越权改名"},
        headers=_auth(world.manager_token),
    )
    assert rename.status_code == 403

    assert _snapshot(api, world) == world.snapshots


# ── P9.2 管理端角色强制 ──────────────────────────────────


def test_admin_memory_endpoints_role_enforcement(api: Api) -> None:
    """管理端记忆接口：manager 可读，staff/guest 403，anonymous 401；PATCH 写操作需 admin。"""
    world = _build_world(api)

    resp = api.client.get("/api/admin/memories", headers=_auth(world.manager_token))
    assert resp.status_code == 200, resp.text

    resp = api.client.get("/api/admin/memories", headers=_auth(world.staff_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/memories", headers=_auth(world.guest_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/memories")
    assert resp.status_code == 401, resp.text

    fake_id = uuid4()
    resp = api.client.patch(
        f"/api/admin/memories/{fake_id}",
        headers=_auth(world.staff_a_token),
        json={"sensitive": True},
    )
    assert resp.status_code == 403, resp.text

    resp = api.client.patch(
        f"/api/admin/memories/{fake_id}",
        headers=_auth(world.manager_token),
        json={"sensitive": True},
    )
    assert resp.status_code == 403, resp.text

    assert _snapshot(api, world) == world.snapshots


def test_admin_agent_config_role_enforcement(api: Api) -> None:
    """管理端 Agent 配置接口：manager 可读，staff/guest 403，anonymous 401；PUT 写操作需 admin。"""
    world = _build_world(api)

    resp = api.client.get("/api/admin/agent-config/agents", headers=_auth(world.manager_token))
    assert resp.status_code == 200, resp.text

    resp = api.client.get("/api/admin/agent-config/agents", headers=_auth(world.staff_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/agent-config/agents", headers=_auth(world.guest_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/agent-config/agents")
    assert resp.status_code == 401, resp.text

    body = {"config_json": {"temperature": 0.5}}
    resp = api.client.put(
        "/api/admin/agent-config/agents/front-desk",
        headers=_auth(world.staff_a_token),
        json=body,
    )
    assert resp.status_code == 403, resp.text

    resp = api.client.put(
        "/api/admin/agent-config/agents/front-desk",
        headers=_auth(world.manager_token),
        json=body,
    )
    assert resp.status_code == 403, resp.text

    assert _snapshot(api, world) == world.snapshots


def test_admin_quality_eval_role_enforcement(api: Api) -> None:
    """管理端质量/评估接口：manager 可读，staff/guest 403，anonymous 401。"""
    world = _build_world(api)

    resp = api.client.get(
        "/api/admin/quality/scorecards/latest", headers=_auth(world.manager_token)
    )
    assert resp.status_code in (200, 404), resp.text

    resp = api.client.get(
        "/api/admin/quality/scorecards/latest", headers=_auth(world.staff_a_token)
    )
    assert resp.status_code == 403, resp.text

    resp = api.client.get(
        "/api/admin/quality/scorecards/latest", headers=_auth(world.guest_a_token)
    )
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/quality/scorecards/latest")
    assert resp.status_code == 401, resp.text

    resp = api.client.get("/api/admin/eval/agent/reports", headers=_auth(world.manager_token))
    assert resp.status_code in (200, 404), resp.text

    resp = api.client.get("/api/admin/eval/agent/reports", headers=_auth(world.staff_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/eval/kb/datasets", headers=_auth(world.manager_token))
    assert resp.status_code in (200, 404), resp.text

    resp = api.client.get("/api/admin/eval/kb/datasets", headers=_auth(world.staff_a_token))
    assert resp.status_code == 403, resp.text

    assert _snapshot(api, world) == world.snapshots


def test_admin_feedback_role_enforcement(api: Api) -> None:
    """管理端反馈接口：manager 可读，staff 403，anonymous 401。"""
    world = _build_world(api)

    resp = api.client.get("/api/admin/feedback/downvoted", headers=_auth(world.manager_token))
    assert resp.status_code in (200, 404), resp.text

    resp = api.client.get("/api/admin/feedback/downvoted", headers=_auth(world.staff_a_token))
    assert resp.status_code == 403, resp.text

    resp = api.client.get("/api/admin/feedback/downvoted")
    assert resp.status_code == 401, resp.text

    assert _snapshot(api, world) == world.snapshots


def test_admin_hotel_config_write_role_enforcement(api: Api) -> None:
    """管理端酒店配置：manager 可读，PUT 写操作需 admin（manager/staff 403，anonymous 401）。"""
    world = _build_world(api)

    resp = api.client.get("/api/admin/hotel-config", headers=_auth(world.manager_token))
    assert resp.status_code in (200, 404), resp.text

    body = {"hotel_name": "越权酒店"}
    resp = api.client.put("/api/admin/hotel-config", headers=_auth(world.manager_token), json=body)
    assert resp.status_code == 403, resp.text

    resp = api.client.put("/api/admin/hotel-config", headers=_auth(world.staff_a_token), json=body)
    assert resp.status_code == 403, resp.text

    resp = api.client.put("/api/admin/hotel-config", json=body)
    assert resp.status_code == 401, resp.text

    assert _snapshot(api, world) == world.snapshots
