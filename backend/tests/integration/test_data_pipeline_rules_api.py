"""数据管道规则种子与校验集成测试（PR-D2）：种子幂等、task_type 白名单、cron 校验。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.db import get_session
from app.main import app
from app.models.data_pipeline import DataCleaningRule
from app.repositories import UserRepository
from app.security import hash_password
from app.seed import CLEANING_RULE_PLAN, seed
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

PASSWORD = "pipe-pass-1"


def test_seed_cleaning_rules_idempotent(runtime_db_url: str) -> None:
    async def _body(factory: SessionFactoryArg) -> dict[str, int]:
        async with factory() as session:
            first = await seed(session)
            second = await seed(session)
        async with factory() as session:
            total = len((await session.execute(select(DataCleaningRule))).scalars().all())
        return {
            "first": first.created.get("cleaning_rules", 0),
            "second": second.created.get("cleaning_rules", 0),
            "total": total,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["first"] == len(CLEANING_RULE_PLAN)
    assert result["second"] == 0, "重复 seed 不应重复插入"
    assert result["total"] == len(CLEANING_RULE_PLAN)


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


def _token(api: Api) -> str:
    username = f"pipe-{uuid4().hex[:8]}"

    async def _body() -> None:
        async with AsyncSession(api.engine, expire_on_commit=False) as session:
            await UserRepository(session).create_account(
                username=username,
                password_hash=hash_password(PASSWORD),
                role="admin",
                display_name="admin",
            )
            await session.commit()

    asyncio.run(_body())
    resp = api.client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    return str(resp.json()["token"])


def test_unknown_task_type_rejected(api: Api) -> None:
    token = _token(api)
    resp = api.client.post(
        "/api/admin/data-pipeline/rules",
        json={"name": "x", "task_type": "data_pipeline.nope"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "未知管道任务类型" in resp.text


def test_invalid_cron_rejected(api: Api) -> None:
    token = _token(api)
    resp = api.client.post(
        "/api/admin/data-pipeline/rules",
        json={
            "name": "x",
            "task_type": "data_pipeline.clean_conversations",
            "schedule_cron": "not a cron",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422
    assert "cron" in resp.text.lower()


def test_valid_rule_created(api: Api) -> None:
    token = _token(api)
    resp = api.client.post(
        "/api/admin/data-pipeline/rules",
        json={
            "name": "会话脱敏",
            "task_type": "data_pipeline.clean_conversations",
            "schedule_cron": "0 3 * * *",
            "config": {"batch_size": 500},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["task_type"] == "data_pipeline.clean_conversations"
    assert resp.json()["config"] == {"batch_size": 500}
