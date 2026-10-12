"""授权基座单元测试：密码散列、会话令牌（fail-closed）与 Bearer 解析。"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from app.security.identity import _bearer_token
from app.security.passwords import hash_password, verify_password
from app.security.session_service import (
    SESSION_KEY_PREFIX,
    SessionService,
    SessionUnavailableError,
)


class FakeRedis:
    """最小可用假客户端：只覆盖会话服务用到的五个命令。"""

    def __init__(self, *, fail_on_get: bool = False) -> None:
        self.store: dict[str, str] = {}
        self.expires: dict[str, int] = {}
        self.deleted: list[str] = []
        self.fail_on_get = fail_on_get

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.expires[key] = ex

    async def get(self, key: str) -> str | None:
        if self.fail_on_get:
            raise ConnectionError("redis down")
        return self.store.get(key)

    async def expire(self, key: str, ttl: int) -> None:
        self.expires[key] = ttl

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.store.pop(key, None)

    async def aclose(self) -> None:
        return None


def test_password_hash_roundtrip() -> None:
    digest = hash_password("s3cret-皓晨")
    assert digest.startswith("$argon2id$")
    assert verify_password("s3cret-皓晨", digest) is True
    assert verify_password("wrong", digest) is False


def test_password_hash_unique_salts_and_broken_hash_rejected() -> None:
    first = hash_password("same-password")
    second = hash_password("same-password")
    assert first != second
    assert verify_password("same-password", None) is False
    assert verify_password("same-password", "") is False
    assert verify_password("same-password", "$argon2id$broken") is False


def test_bearer_token_parsing() -> None:
    assert _bearer_token("Bearer abc") == "abc"
    assert _bearer_token("bearer abc") == "abc"
    assert _bearer_token("Bearer   abc  ") == "abc"
    assert _bearer_token(None) is None
    assert _bearer_token("") is None
    assert _bearer_token("Basic abc") is None
    assert _bearer_token("Bearer   ") is None


def test_session_issue_resolve_and_sliding_renewal() -> None:
    async def _body() -> None:
        client = FakeRedis()
        service = SessionService(client, ttl_seconds=600)
        user_id = uuid4()
        token = await service.create(user_id)
        assert len(token) >= 32
        key = f"{SESSION_KEY_PREFIX}{token}"
        assert client.store[key] == str(user_id)
        assert client.expires[key] == 600

        assert await service.resolve(token) == user_id
        assert client.expires[key] == 600

        assert await service.resolve("not-a-token") is None
        assert await service.resolve("") is None

    asyncio.run(_body())


def test_session_tokens_are_unguessable_and_distinct() -> None:
    async def _body() -> None:
        service = SessionService(FakeRedis())
        user_id = uuid4()
        tokens = {await service.create(user_id) for _ in range(5)}
        assert len(tokens) == 5

    asyncio.run(_body())


def test_session_revoke_invalidates_token() -> None:
    async def _body() -> None:
        client = FakeRedis()
        service = SessionService(client)
        token = await service.create(uuid4())
        await service.revoke(token)
        assert f"{SESSION_KEY_PREFIX}{token}" in client.deleted
        assert await service.resolve(token) is None

    asyncio.run(_body())


def test_session_fail_closed_without_redis() -> None:
    async def _body() -> None:
        service = SessionService(None)
        assert service.enabled is False
        assert await service.resolve("any-token") is None
        with pytest.raises(SessionUnavailableError):
            await service.create(uuid4())
        await service.revoke("any-token")
        await service.aclose()

    asyncio.run(_body())


def test_session_resolve_fails_closed_on_redis_error() -> None:
    async def _body() -> None:
        service = SessionService(FakeRedis(fail_on_get=True))
        assert await service.resolve("any-token") is None

    asyncio.run(_body())


def test_session_resolve_rejects_malformed_value() -> None:
    async def _body() -> None:
        client = FakeRedis()
        service = SessionService(client)
        client.store[f"{SESSION_KEY_PREFIX}token"] = "not-a-uuid"
        assert await service.resolve("token") is None

    asyncio.run(_body())
