"""保洁客评回归测试（真实 PostgreSQL + TestClient）。

背景：feedback_service.FeedbackCollector.get_staff_ratings 读取
housekeeping_tasks.guest_score / guest_comment，但两列从未落地——接口一经真实调用即报错。
本套用例锁定修复：列已补齐、客人侧可写入评分、反馈读接口可返回已评任务。

覆盖：
- 反馈读接口不再因缺列报错，且能捞取已评任务；
- 客人对自己发起的已完成任务评分（端到端，含归属校验）；
- 未交付任务不可评（状态守卫）；
- 越界评分被拒（领域校验）；
- 非本人任务评分被拒（归属 fail-closed）；
- 未认证一律 401（A6 取证）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.domain.errors import ConflictError, ValidationFailedError
from app.main import app
from app.repositories import UserRepository
from app.security import hash_password
from app.services.feedback_service import FeedbackCollector
from app.services.housekeeping_service import HousekeepingService
from tests.integration.db_utils import run_with_session
from tests.integration.test_room_service import _seed_guest_user, _seed_room_and_order

pytestmark = pytest.mark.integration

DELIVER_PATH = ("accepted", "in_progress", "done", "verified")


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


def _run(api: Api, body: Any) -> Any:
    async def _inner() -> Any:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            result = await body(session)
            await session.commit()
            return result

    return asyncio.run(_inner())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _customer_token(api: Api, *, password: str = "guest-pass-123") -> str:
    username = f"rate-{uuid4().hex[:8]}"

    async def _body(session: AsyncSession) -> None:
        await UserRepository(session).create_account(
            username=username, password_hash=hash_password(password), role="customer"
        )

    _run(api, _body)
    resp = api.client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["token"])


def _seed_room(api: Api) -> str:
    """造一个可用房间（含房型），返回 room_no。"""
    room_no: str = ""

    async def _body(session: AsyncSession) -> str:
        _, guest_id = await _seed_guest_user(session, username=f"seed-{uuid4().hex[:8]}")
        _, room_no_val, _ = await _seed_room_and_order(session, guest_id=guest_id)
        return room_no_val

    room_no = _run(api, _body)
    return room_no


def _advance_to_verified(api: Api, task_id: UUID) -> None:
    async def _body(session: AsyncSession) -> None:
        svc = HousekeepingService(session)
        for status in DELIVER_PATH:
            await svc.transition(task_id, status)

    _run(api, _body)


# ── 反馈读接口回归 ────────────────────────────────────────────────


def test_get_staff_ratings_no_longer_errors(runtime_db_url: str) -> None:
    """核心回归：反馈读接口不再因缺列报错，且能捞取已评任务。"""

    async def _body(session: AsyncSession) -> dict[str, Any]:
        _, guest_id = await _seed_guest_user(session, username=f"rate-{uuid4().hex[:8]}")
        _, room_no, room_id = await _seed_room_and_order(session, guest_id=guest_id)
        svc = HousekeepingService(session)
        task = await svc.create(
            task_type="call_clean",
            room_id=room_id,
            room_no=room_no,
            payload={"requested_by": str(guest_id)},
        )
        for status in DELIVER_PATH:
            await svc.transition(task.id, status)
        await svc.rate(task.id, score=5, comment="很干净")
        await session.commit()

        collector = FeedbackCollector(session)
        ratings = await collector.get_staff_ratings()
        return {
            "count": len(ratings),
            "score": ratings[0]["score"] if ratings else None,
            "comment": ratings[0]["comment"] if ratings else None,
            "rated_at": ratings[0]["rated_at"] if ratings else None,
        }

    result = run_with_session(runtime_db_url, _body)
    assert result["count"] >= 1
    assert result["score"] == 5.0
    assert result["comment"] == "很干净"
    assert result["rated_at"] is not None


# ── 客人评分写入端到端 ────────────────────────────────────────────


def test_guest_rates_own_task_end_to_end(api: Api) -> None:
    """端到端：登录客人自己发起清洁 → 施工完成 → 评分成功并回显。"""
    token = _customer_token(api)
    room_no = _seed_room(api)

    created = api.client.post(
        "/api/guest/services/clean",
        json={"room_no": room_no},
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    task_id = UUID(created.json()["id"])

    _advance_to_verified(api, task_id)

    rated = api.client.post(
        f"/api/guest/services/clean/{task_id}/rating",
        json={"score": 4, "comment": "打扫得不错"},
        headers=_auth(token),
    )
    assert rated.status_code == 200, rated.text
    payload = rated.json()
    assert payload["guest_score"] == 4
    assert payload["guest_comment"] == "打扫得不错"
    assert payload["status"] == "verified"
    assert payload["rated_at"] is not None


def test_guest_cannot_rate_others_task(api: Api) -> None:
    """非本人任务评分被拒：fail-closed 403。"""
    owner_token = _customer_token(api)
    stranger_token = _customer_token(api)
    room_no = _seed_room(api)

    created = api.client.post(
        "/api/guest/services/clean",
        json={"room_no": room_no},
        headers=_auth(owner_token),
    )
    assert created.status_code == 201, created.text
    task_id = UUID(created.json()["id"])
    _advance_to_verified(api, task_id)

    resp = api.client.post(
        f"/api/guest/services/clean/{task_id}/rating",
        json={"score": 5},
        headers=_auth(stranger_token),
    )
    assert resp.status_code == 403, resp.text


def test_rate_requires_auth(api: Api) -> None:
    """未认证一律 401（A6 取证）。"""
    resp = api.client.post(
        f"/api/guest/services/clean/{uuid4()}/rating",
        json={"score": 5},
    )
    assert resp.status_code == 401, resp.text


# ── 领域守卫 ─────────────────────────────────────────────────────


def test_rate_rejected_before_delivery(runtime_db_url: str) -> None:
    """未交付任务不可评（状态守卫）。"""

    async def _body(session: AsyncSession) -> str:
        _, guest_id = await _seed_guest_user(session, username=f"rate-{uuid4().hex[:8]}")
        _, room_no, room_id = await _seed_room_and_order(session, guest_id=guest_id)
        svc = HousekeepingService(session)
        task = await svc.create(
            task_type="call_clean",
            room_id=room_id,
            room_no=room_no,
            payload={"requested_by": str(guest_id)},
        )
        await session.commit()
        with pytest.raises(ConflictError):
            await svc.rate(task.id, score=5)
        return task.status

    status = run_with_session(runtime_db_url, _body)
    assert status == "pending"


def test_rate_rejected_out_of_range(runtime_db_url: str) -> None:
    """越界评分被拒（领域校验）。"""

    async def _body(session: AsyncSession) -> None:
        _, guest_id = await _seed_guest_user(session, username=f"rate-{uuid4().hex[:8]}")
        _, room_no, room_id = await _seed_room_and_order(session, guest_id=guest_id)
        svc = HousekeepingService(session)
        task = await svc.create(
            task_type="call_clean",
            room_id=room_id,
            room_no=room_no,
            payload={"requested_by": str(guest_id)},
        )
        for status in DELIVER_PATH:
            await svc.transition(task.id, status)
        with pytest.raises(ValidationFailedError):
            await svc.rate(task.id, score=9)
        with pytest.raises(ValidationFailedError):
            await svc.rate(task.id, score=0)

    run_with_session(runtime_db_url, _body)
