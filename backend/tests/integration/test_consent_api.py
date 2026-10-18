"""隐私告知与同意记录集成测试（真实 HTTP + PostgreSQL）。

覆盖：公开告知、未认证拒绝、同意/撤回追加、重复提交去重、版本过期拒绝、
未知用途拒绝、跨用户隔离。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models.privacy import ConsentRecord
from app.repositories import UserRepository
from app.security import hash_password

pytestmark = pytest.mark.integration

PASSWORD = "consent-pass-1"


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


def _seed_user(api: Api, *, username: str) -> None:
    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(PASSWORD),
                role="customer",
                display_name=username,
            )
            await session.commit()

    asyncio.run(_body())


def _login(api: Api, *, username: str) -> str:
    response = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _consent_count(api: Api) -> int:
    async def _query() -> int:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            result = await session.execute(select(func.count()).select_from(ConsentRecord))
            return int(result.scalar_one())

    return asyncio.run(_query())


def _consents(api: Api, headers: dict[str, str]) -> dict[str, dict[str, object]]:
    response = api.client.get("/api/privacy/consents", headers=headers)
    assert response.status_code == 200, response.text
    return {item["purpose"]: item for item in response.json()["items"]}


def test_privacy_notice_is_public(api: Api) -> None:
    response = api.client.get("/api/privacy/notice")
    assert response.status_code == 200
    payload = response.json()
    assert payload["policy_version"] == get_settings().privacy_policy_version
    purposes = {item["purpose"]: item for item in payload["purposes"]}
    assert purposes["privacy_policy"]["required"] is True
    assert purposes["face_recognition"]["required"] is False
    assert payload["data_categories"]
    assert payload["retention"]
    assert "境外" in payload["cross_border"]


def test_consent_requires_authentication(api: Api) -> None:
    assert api.client.get("/api/privacy/consents").status_code == 401
    assert (
        api.client.post(
            "/api/privacy/consents",
            json={"purpose": "privacy_policy", "granted": True},
        ).status_code
        == 401
    )


def test_consent_lifecycle_is_append_only(api: Api) -> None:
    username = f"consent-{uuid4().hex[:8]}"
    _seed_user(api, username=username)
    headers = _auth(_login(api, username=username))

    initial = _consents(api, headers)
    assert all(item["granted"] is False for item in initial.values())
    version = str(initial["privacy_policy"]["policy_version"] or "")

    granted = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "privacy_policy", "granted": True},
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()["granted"] is True
    assert granted.json()["policy_version"] == get_settings().privacy_policy_version

    # 幂等：相同状态重复提交不新增事件
    repeated = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "privacy_policy", "granted": True},
    )
    assert repeated.status_code == 201
    assert _consent_count(api) == 1

    marketing_on = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "marketing", "granted": True},
    )
    assert marketing_on.status_code == 201
    marketing_off = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "marketing", "granted": False},
    )
    assert marketing_off.status_code == 201
    assert _consent_count(api) == 3

    state = _consents(api, headers)
    assert state["privacy_policy"]["granted"] is True
    assert state["marketing"]["granted"] is False

    stale = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "marketing", "granted": True, "policy_version": version + "-stale"},
    )
    assert stale.status_code == 409
    assert _consent_count(api) == 3

    unknown = api.client.post(
        "/api/privacy/consents",
        headers=headers,
        json={"purpose": "ad_tracking", "granted": True},
    )
    assert unknown.status_code == 422


def test_consent_is_isolated_between_users(api: Api) -> None:
    first = f"consent-a-{uuid4().hex[:8]}"
    second = f"consent-b-{uuid4().hex[:8]}"
    _seed_user(api, username=first)
    _seed_user(api, username=second)

    first_headers = _auth(_login(api, username=first))
    second_headers = _auth(_login(api, username=second))

    granted = api.client.post(
        "/api/privacy/consents",
        headers=first_headers,
        json={"purpose": "privacy_policy", "granted": True},
    )
    assert granted.status_code == 201

    assert _consents(api, first_headers)["privacy_policy"]["granted"] is True
    assert _consents(api, second_headers)["privacy_policy"]["granted"] is False
    assert _consent_count(api) == 1
