"""集成测试共用执行器：每次调用独立事件循环 + NullPool，避免跨循环连接复用。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


def _new_engine(db_url: str) -> AsyncEngine:
    return create_async_engine(db_url, poolclass=NullPool)


def run_with_engine(db_url: str, fn: Callable[[AsyncEngine], Coroutine[Any, Any, Any]]) -> Any:
    async def _inner() -> Any:
        engine = _new_engine(db_url)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def run_with_connection(
    db_url: str, fn: Callable[[AsyncConnection], Coroutine[Any, Any, Any]]
) -> Any:
    async def _inner() -> Any:
        engine = _new_engine(db_url)
        try:
            async with engine.connect() as conn:
                return await fn(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def run_with_session(db_url: str, fn: Callable[[AsyncSession], Coroutine[Any, Any, Any]]) -> Any:
    async def _inner() -> Any:
        engine = _new_engine(db_url)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                return await fn(session)
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def run_with_session_factory(
    db_url: str, fn: Callable[[async_sessionmaker[AsyncSession]], Coroutine[Any, Any, Any]]
) -> Any:
    """在单事件循环内提供会话工厂：供被测服务与测试代码并发使用独立连接。"""

    async def _inner() -> Any:
        engine = _new_engine(db_url)
        try:
            return await fn(async_sessionmaker(engine, expire_on_commit=False))
        finally:
            await engine.dispose()

    return asyncio.run(_inner())


def inspect_tables(conn: AsyncConnection) -> dict[str, dict[str, Any]]:
    def _collect(sync_conn: Any) -> dict[str, dict[str, Any]]:
        inspector = inspect(sync_conn)
        result: dict[str, dict[str, Any]] = {}
        for table in inspector.get_table_names():
            result[table] = {
                "columns": {col["name"]: col for col in inspector.get_columns(table)},
                "unique_constraints": {
                    uc["name"] for uc in inspector.get_unique_constraints(table)
                },
                "indexes": {idx["name"]: idx for idx in inspector.get_indexes(table)},
            }
        return result

    return conn.run_sync(_collect)
