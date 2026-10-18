"""P4.4 容量参数与审计合并写入：配置解析、非法值失败关闭、并发事实共用事务。"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest

from app.agents.runtime import CheckpointPoolSettings, resolve_checkpoint_pool_settings
from app.config import Settings
from app.services import audit_service
from app.services.audit_service import AuditRecorder


def test_checkpoint_pool_defaults() -> None:
    assert resolve_checkpoint_pool_settings(Settings()) == CheckpointPoolSettings(
        min_size=1, max_size=4, open_timeout_seconds=10.0
    )


def test_checkpoint_pool_reads_overrides() -> None:
    settings = Settings(
        checkpoint_pool_min_size=2,
        checkpoint_pool_max_size=12,
        checkpoint_pool_open_timeout_seconds=3.5,
    )
    assert resolve_checkpoint_pool_settings(settings) == CheckpointPoolSettings(
        min_size=2, max_size=12, open_timeout_seconds=3.5
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"checkpoint_pool_min_size": 0},
        {"checkpoint_pool_min_size": 5, "checkpoint_pool_max_size": 4},
        {"checkpoint_pool_open_timeout_seconds": 0},
        {"message_history_limit": 0},
        {"audit_batch_window_ms": -1},
        {"audit_batch_max_facts": 0},
    ],
)
def test_capacity_settings_reject_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Settings(**overrides)


class _FakeSession:
    def __init__(self, factory: _FakeFactory) -> None:
        self._factory = factory

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def commit(self) -> None:
        self._factory.commits += 1


class _FakeFactory:
    def __init__(self) -> None:
        self.commits = 0
        self.facts: list[tuple[str, dict[str, Any]]] = []

    def __call__(self) -> _FakeSession:
        return _FakeSession(self)


class _FakeRepository:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def append_model(self, **kwargs: Any) -> None:
        self._session._factory.facts.append(("model", kwargs))

    async def append_tool(self, **kwargs: Any) -> None:
        self._session._factory.facts.append(("tool", kwargs))

    async def append_route(self, **kwargs: Any) -> None:
        self._session._factory.facts.append(("route", kwargs))


@pytest.fixture
def fake_factory(monkeypatch: pytest.MonkeyPatch) -> _FakeFactory:
    monkeypatch.setattr(audit_service, "AuditRepository", _FakeRepository)
    return _FakeFactory()


def _recorder(factory: _FakeFactory, *, window_ms: int = 1, max_facts: int = 32) -> AuditRecorder:
    return AuditRecorder(
        run_id=uuid4(),
        agent_slug="front-desk",
        session_factory=cast(Any, factory),
        batch_window_ms=window_ms,
        batch_max_facts=max_facts,
    )


def test_concurrent_facts_share_one_transaction(fake_factory: _FakeFactory) -> None:
    async def _run() -> None:
        recorder = _recorder(fake_factory)
        derived = recorder.with_agent("order")
        calls = [
            recorder.tool_call(tool_name=f"tool_{index}", status="completed") for index in range(3)
        ] + [
            derived.tool_call(tool_name=f"derived_{index}", status="completed")
            for index in range(2)
        ]
        await asyncio.gather(*calls)

    asyncio.run(_run())
    assert fake_factory.commits == 1
    assert sorted(fact[1]["tool_name"] for fact in fake_factory.facts) == [
        "derived_0",
        "derived_1",
        "tool_0",
        "tool_1",
        "tool_2",
    ]


def test_sequential_facts_commit_separately(fake_factory: _FakeFactory) -> None:
    async def _run() -> None:
        recorder = _recorder(fake_factory)
        for index in range(3):
            await recorder.tool_call(tool_name=f"tool_{index}", status="completed")

    asyncio.run(_run())
    assert fake_factory.commits == 3


def test_batch_size_splits_transactions(fake_factory: _FakeFactory) -> None:
    async def _run() -> None:
        recorder = _recorder(fake_factory, max_facts=2)
        calls = [
            recorder.tool_call(tool_name=f"tool_{index}", status="completed") for index in range(5)
        ]
        await asyncio.gather(*calls)

    asyncio.run(_run())
    assert fake_factory.commits == 3
    assert len(fake_factory.facts) == 5


def test_zero_window_disables_batching(fake_factory: _FakeFactory) -> None:
    async def _run() -> None:
        recorder = _recorder(fake_factory, window_ms=0)
        calls = [
            recorder.tool_call(tool_name=f"tool_{index}", status="completed") for index in range(4)
        ]
        await asyncio.gather(*calls)

    asyncio.run(_run())
    assert fake_factory.commits == 4
