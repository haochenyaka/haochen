"""集成测试夹具：会话级临时数据库 + Alembic 迁移；PostgreSQL 不可达时整组跳过。"""

from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from app.config import get_settings

if sys.platform == "win32":  # psycopg 异步驱动不支持 Windows 默认的 Proactor 事件循环
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _maintenance_dsn(database_url: str) -> str:
    return (
        make_url(database_url)
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False)
    )


async def _execute_admin_sql(database_url: str, sql: str) -> None:
    conn = await asyncpg.connect(_maintenance_dsn(database_url))
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def runtime_db_url() -> Iterator[str]:
    """创建一次性数据库并迁移到最新版本，产出可直接连接的 URL。"""
    database_url = get_settings().database_url
    temp_name = f"haochen_test_{secrets.token_hex(4)}"
    try:
        asyncio.run(_execute_admin_sql(database_url, f'CREATE DATABASE "{temp_name}"'))
    except OSError as exc:
        pytest.skip(f"PostgreSQL 不可达，跳过集成测试：{exc}")

    temp_url = make_url(database_url).set(database=temp_name).render_as_string(hide_password=False)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env={**os.environ, "DATABASE_URL": temp_url},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"alembic upgrade head 失败：\n{result.stdout}\n{result.stderr}")
        yield temp_url
    finally:
        asyncio.run(
            _execute_admin_sql(database_url, f'DROP DATABASE IF EXISTS "{temp_name}" WITH (FORCE)')
        )


_TRUNCATE_EXCLUDE = (
    "alembic_version",
    "spatial_ref_sys",
    "checkpoint_migrations",
)


@pytest.fixture(autouse=True)
def _isolate_db(request: pytest.FixtureRequest) -> Iterator[None]:
    """每个集成用例前后清空业务表：集成库为会话级共享，跨用例残留会导致
    "全库重算"类断言（报表/评估/凭证版本/健康面板）偶发失败。

    用例执行前后各清一次（前置清防止污染泄漏到依赖来源，后置清是主要隔离手段）。
    非集成用例（不依赖 runtime_db_url）直接跳过，不做任何事。"""
    if "runtime_db_url" not in request.fixturenames:
        yield
        return
    raw_url = request.getfixturevalue("runtime_db_url")
    # asyncpg 只认 postgresql/postgres scheme，去掉 SQLAlchemy 的驱动后缀
    pg_url = make_url(raw_url).set(drivername="postgresql").render_as_string(hide_password=False)

    def _run(reset: bool) -> None:
        async def _inner() -> None:
            conn = await asyncpg.connect(pg_url)
            try:
                if reset:
                    seq = "pg_get_serial_sequence('checkpoint_migrations', 'v')"
                    await conn.execute(f"SELECT setval({seq}, 1, false)")
                tables = await conn.fetch(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                )
                names = [
                    row["tablename"] for row in tables if row["tablename"] not in _TRUNCATE_EXCLUDE
                ]
                if names:
                    quoted = ", ".join(f'"{name}"' for name in names)
                    await conn.execute(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE")
            finally:
                await conn.close()

        asyncio.run(_inner())

    _run(reset=True)
    try:
        yield
    finally:
        _run(reset=False)
