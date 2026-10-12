"""基础数据管理接口集成测试：角色边界（admin 写 / manager 读 / 其余拒绝）、取值校验与账号管理。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.repositories import UserRepository
from app.security import hash_password

pytestmark = pytest.mark.integration


@dataclass
class Api:
    client: TestClient
    engine: AsyncEngine


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


def _seed_role(api: Api, *, role: str, password: str = "pass-123456") -> tuple[str, str]:
    username = f"{role}-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(password),
                role=role,
                display_name=role,
            )
            await session.commit()

    asyncio.run(_body())
    response = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return str(response.json()["token"]), username


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_room_type(api: Api, token: str, *, code: str | None = None) -> dict[str, object]:
    response = api.client.post(
        "/api/admin/room-types",
        headers=_auth(token),
        json={
            "code": code or f"RT-{uuid4().hex[:6].upper()}",
            "name": "测试房型",
            "base_price_fen": 29900,
            "capacity": 2,
            "bed_type": "king",
            "amenities": ["wifi"],
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def test_admin_can_manage_room_types(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    created = _create_room_type(api, token)
    assert created["active"] is True
    assert created["base_price_fen"] == 29900

    listed = api.client.get("/api/admin/room-types", headers=_auth(token))
    assert listed.status_code == 200
    assert any(row["id"] == created["id"] for row in listed.json())

    patched = api.client.patch(
        f"/api/admin/room-types/{created['id']}",
        headers=_auth(token),
        json={"base_price_fen": 31900, "active": False},
    )
    assert patched.status_code == 200
    assert patched.json()["base_price_fen"] == 31900
    assert patched.json()["active"] is False

    hidden = api.client.get("/api/admin/room-types", headers=_auth(token))
    assert all(row["id"] != created["id"] for row in hidden.json())
    visible = api.client.get("/api/admin/room-types?include_inactive=true", headers=_auth(token))
    assert any(row["id"] == created["id"] for row in visible.json())

    duplicate = api.client.post(
        "/api/admin/room-types",
        headers=_auth(token),
        json={
            "code": created["code"],
            "name": "重复房型",
            "base_price_fen": 100,
            "capacity": 1,
            "bed_type": "twin",
        },
    )
    assert duplicate.status_code == 409


def test_rooms_require_existing_type_and_valid_status(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    room_type = _create_room_type(api, token)

    created = api.client.post(
        "/api/admin/rooms",
        headers=_auth(token),
        json={
            "room_no": f"T{uuid4().hex[:4]}",
            "room_type_id": room_type["id"],
            "floor": 6,
            "lock_device_no": "LOCK-TEST",
        },
    )
    assert created.status_code == 201, created.text
    room_id = created.json()["id"]

    missing_type = api.client.post(
        "/api/admin/rooms",
        headers=_auth(token),
        json={"room_no": f"T{uuid4().hex[:4]}", "room_type_id": str(uuid4()), "floor": 6},
    )
    assert missing_type.status_code == 404

    bad_status = api.client.patch(
        f"/api/admin/rooms/{room_id}", headers=_auth(token), json={"status": "sleeping"}
    )
    assert bad_status.status_code == 422

    patched = api.client.patch(
        f"/api/admin/rooms/{room_id}", headers=_auth(token), json={"status": "maintenance"}
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "maintenance"


def test_manager_reads_but_cannot_write_basic_data(api: Api) -> None:
    admin_token, _ = _seed_role(api, role="admin")
    manager_token, _ = _seed_role(api, role="manager")
    _create_room_type(api, admin_token)

    read = api.client.get("/api/admin/room-types", headers=_auth(manager_token))
    assert read.status_code == 200
    assert read.json()

    write = api.client.post(
        "/api/admin/room-types",
        headers=_auth(manager_token),
        json={
            "code": f"RT-{uuid4().hex[:6].upper()}",
            "name": "店长越权",
            "base_price_fen": 100,
            "capacity": 1,
            "bed_type": "twin",
        },
    )
    assert write.status_code == 403
    patch = api.client.patch(
        f"/api/admin/room-types/{uuid4()}", headers=_auth(manager_token), json={"name": "改名"}
    )
    assert patch.status_code == 403


def test_operator_and_staff_and_anonymous_are_rejected(api: Api) -> None:
    operator_token, _ = _seed_role(api, role="operator")
    staff_token, _ = _seed_role(api, role="staff")

    for token in (operator_token, staff_token):
        assert api.client.get("/api/admin/room-types", headers=_auth(token)).status_code == 403
        assert api.client.get("/api/admin/users", headers=_auth(token)).status_code == 403

    assert api.client.get("/api/admin/room-types").status_code == 401
    assert api.client.get("/api/admin/users").status_code == 401


def test_account_management_and_password_reset(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    username = f"created-{uuid4().hex[:8]}"

    created = api.client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={
            "username": username,
            "password": "initial-pass-1",
            "role": "operator",
            "display_name": "新客服",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["role"] == "operator"
    assert "password_hash" not in body
    assert (
        api.client.post(
            "/api/auth/login", json={"username": username, "password": "initial-pass-1"}
        ).status_code
        == 200
    )

    invalid_role = api.client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={
            "username": f"bad-{uuid4().hex[:8]}",
            "password": "another-pass-1",
            "role": "superuser",
        },
    )
    assert invalid_role.status_code == 422

    reset = api.client.patch(
        f"/api/admin/users/{body['id']}",
        headers=_auth(token),
        json={"password": "rotated-pass-1", "display_name": "改密后"},
    )
    assert reset.status_code == 200
    assert reset.json()["display_name"] == "改密后"
    assert (
        api.client.post(
            "/api/auth/login", json={"username": username, "password": "rotated-pass-1"}
        ).status_code
        == 200
    )
    assert (
        api.client.post(
            "/api/auth/login", json={"username": username, "password": "initial-pass-1"}
        ).status_code
        == 401
    )

    disabled = api.client.patch(
        f"/api/admin/users/{body['id']}", headers=_auth(token), json={"status": "disabled"}
    )
    assert disabled.status_code == 200
    assert (
        api.client.post(
            "/api/auth/login", json={"username": username, "password": "rotated-pass-1"}
        ).status_code
        == 403
    )


def test_admin_cannot_disable_own_account(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    me = api.client.get("/api/auth/me", headers=_auth(token)).json()
    response = api.client.patch(
        f"/api/admin/users/{me['id']}", headers=_auth(token), json={"status": "disabled"}
    )
    assert response.status_code == 409


def test_staff_directory_create_and_link_account(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    username = f"linked-{uuid4().hex[:8]}"
    account = api.client.post(
        "/api/admin/users",
        headers=_auth(token),
        json={"username": username, "password": "staff-pass-1", "role": "staff"},
    )
    assert account.status_code == 201
    staff_no = f"HK-{uuid4().hex[:6].upper()}"

    created = api.client.post(
        "/api/admin/staff",
        headers=_auth(token),
        json={
            "staff_no": staff_no,
            "name": "测试保洁",
            "user_id": account.json()["id"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["position"] == "housekeeping"
    assert created.json()["status"] == "active"

    duplicate = api.client.post(
        "/api/admin/staff", headers=_auth(token), json={"staff_no": staff_no, "name": "重复工号"}
    )
    assert duplicate.status_code == 409

    unknown_position = api.client.patch(
        f"/api/admin/staff/{created.json()['id']}",
        headers=_auth(token),
        json={"position": "reception"},
    )
    assert unknown_position.status_code == 422

    listed = api.client.get("/api/admin/staff", headers=_auth(token))
    assert listed.status_code == 200
    assert any(row["staff_no"] == staff_no for row in listed.json())


def _assert_not_found(api: Api, token: str, path: str, payload: dict[str, object]) -> None:
    response = api.client.patch(path, headers=_auth(token), json=payload)
    assert response.status_code == 404


def test_patch_unknown_identifiers(api: Api) -> None:
    token, _ = _seed_role(api, role="admin")
    unknown = str(uuid4())
    _assert_not_found(api, token, f"/api/admin/room-types/{unknown}", {"name": "x"})
    _assert_not_found(api, token, f"/api/admin/rooms/{unknown}", {"floor": 1})
    _assert_not_found(api, token, f"/api/admin/staff/{unknown}", {"name": "x"})
    _assert_not_found(api, token, f"/api/admin/users/{unknown}", {"display_name": "x"})
