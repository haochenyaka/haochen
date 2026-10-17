"""P4.3 可观测与告警：SSE 指标桶、告警规则评估、Langfuse 外发边界。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import langfuse
import pytest

from app.config import Settings
from app.services.alert_service import (
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_WARNING,
    AlertService,
)
from app.services.langfuse_service import (
    EXPORT_DISABLED,
    EXPORT_MASKED_EXTERNAL,
    EXPORT_SELF_HOSTED,
    LangfuseClient,
    get_langfuse,
    mask_identifier,
)
from app.services.observability_service import (
    InMemorySseMetrics,
    PoolStats,
    RedisSseMetrics,
    database_pool_stats,
    sse_metric_key,
)

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self._client = client
        self._ops: list[tuple[str, ...]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def hincrby(self, key: str, field: str, amount: int) -> None:
        self._ops.append(("hincrby", key, field, str(amount)))

    def expire(self, key: str, ttl: int) -> None:
        self._ops.append(("expire", key, str(ttl)))

    def hgetall(self, key: str) -> None:
        self._ops.append(("hgetall", key))

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for op in self._ops:
            if op[0] == "hincrby":
                bucket = self._client.data.setdefault(op[1], {})
                bucket[op[2]] = bucket.get(op[2], 0) + int(op[3])
            elif op[0] == "hgetall":
                results.append(dict(self._client.data.get(op[1], {})))
        self._ops.clear()
        return results


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, int]] = {}
        self.closed = False

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self)

    async def aclose(self) -> None:
        self.closed = True


class _FakeSession:
    def __init__(self, scalars: list[int]) -> None:
        self._scalars = scalars
        self.statements: list[Any] = []

    async def scalar(self, stmt: Any) -> int:
        self.statements.append(stmt)
        return self._scalars.pop(0)


class _FakePool:
    def __init__(self, *, size: int, checked_out: int, overflow: int = 0) -> None:
        self._size = size
        self._checked_out = checked_out
        self._overflow = overflow

    def size(self) -> int:
        return self._size

    def checkedout(self) -> int:
        return self._checked_out

    def overflow(self) -> int:
        return self._overflow


class _UnsupportedPool:
    pass


def test_in_memory_metrics_window_and_rate() -> None:
    clock = [1_000_000.0]
    metrics = InMemorySseMetrics(ttl_seconds=120, clock=lambda: clock[0])

    async def _body() -> None:
        for _ in range(4):
            await metrics.record("stream_started")
        await metrics.record("stream_completed", 2)
        await metrics.record("client_disconnect")
        await metrics.record("follow_error")
        snapshot = await metrics.snapshot(window_minutes=1)
        assert snapshot.started == 4
        assert snapshot.completed == 2
        assert snapshot.abnormal == 1
        assert snapshot.storage_errors == 1
        assert snapshot.disconnect_rate == 0.25

        clock[0] += 180
        expired = await metrics.snapshot(window_minutes=1)
        assert expired.started == 0
        await metrics.record("stream_started")
        assert (await metrics.snapshot(window_minutes=1)).started == 1

    asyncio.run(_body())


def test_redis_metrics_bucket_and_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.observability_service.time.time", lambda: 1_000_000.0)
    client = _FakeRedis()
    metrics = RedisSseMetrics(client, ttl_seconds=600)  # type: ignore[arg-type]

    async def _body() -> None:
        await metrics.record("stream_started", 3)
        await metrics.record("client_disconnect")
        await metrics.record("stream_error")
        snapshot = await metrics.snapshot(window_minutes=1)
        assert snapshot.started == 3
        assert snapshot.client_disconnects == 1
        assert snapshot.stream_errors == 1
        assert snapshot.disconnect_rate == pytest.approx(2 / 3)
        assert sse_metric_key(16_666) in client.data
        await metrics.aclose()
        assert client.closed

    asyncio.run(_body())


def test_stream_safe_paths_record_bus_failures() -> None:
    from uuid import uuid4

    from app.services.run_stream import RunStreamService

    class _FailingBus:
        async def read(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

        async def follow(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("redis down")

    metrics = InMemorySseMetrics()
    service = RunStreamService(
        bus=_FailingBus(),  # type: ignore[arg-type]
        metrics=metrics,
        follow_block_ms=0,
    )

    async def _body() -> None:
        run_id = uuid4()
        assert await service._safe_read(run_id, after=None) == []
        assert await service._safe_follow(run_id, after=None, block_ms=0) == []
        snapshot = await metrics.snapshot(window_minutes=1)
        assert snapshot.read_errors == 1
        assert snapshot.follow_errors == 1

    asyncio.run(_body())


def test_database_pool_stats_reads_queue_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Engine:
        pool = _FakePool(size=10, checked_out=9, overflow=2)

    monkeypatch.setattr("app.services.observability_service.engine", _Engine())
    stats = database_pool_stats()
    assert stats == PoolStats(size=10, checked_out=9, overflow=2)
    assert stats is not None
    assert stats.utilization == 0.9

    class _UnsupportedEngine:
        pool = _UnsupportedPool()

    monkeypatch.setattr("app.services.observability_service.engine", _UnsupportedEngine())
    assert database_pool_stats() is None


def test_alert_report_is_ok_without_samples() -> None:
    session = _FakeSession([0, 0, 0, 0, 0])
    service = AlertService(
        session,  # type: ignore[arg-type]
        metrics=InMemorySseMetrics(),
        pool_stats=PoolStats(size=10, checked_out=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    assert report.status == STATUS_OK
    assert [rule.rule for rule in report.rules] == [
        "run_stuck",
        "lease_expiry_rate",
        "sse_disconnect",
        "db_pool_exhaustion",
    ]
    assert all(not rule.triggered for rule in report.rules)


def test_alert_run_stuck_and_pool_are_critical() -> None:
    session = _FakeSession([2, 1, 1, 0, 0])
    service = AlertService(
        session,  # type: ignore[arg-type]
        metrics=InMemorySseMetrics(),
        pool_stats=PoolStats(size=10, checked_out=10, overflow=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    assert report.status == STATUS_CRITICAL
    stuck = next(rule for rule in report.rules if rule.rule == "run_stuck")
    assert stuck.triggered
    assert stuck.severity == "critical"
    assert stuck.facts["pending_stuck"] == 2
    assert stuck.facts["running_stuck"] == 1
    assert stuck.facts["expired_leases"] == 1

    pool = next(rule for rule in report.rules if rule.rule == "db_pool_exhaustion")
    assert pool.triggered
    assert pool.value == 1.0


def test_alert_lease_expiry_rate_requires_sample() -> None:
    service = AlertService(
        _FakeSession([0, 0, 0, 4, 4]),  # type: ignore[arg-type]
        metrics=InMemorySseMetrics(),
        pool_stats=PoolStats(size=10, checked_out=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    lease = next(rule for rule in report.rules if rule.rule == "lease_expiry_rate")
    assert lease.value == 1.0
    assert not lease.triggered
    assert lease.facts["enough_samples"] is False


def test_alert_lease_expiry_rate_fires_on_spike() -> None:
    service = AlertService(
        _FakeSession([0, 0, 0, 100, 8]),  # type: ignore[arg-type]
        metrics=InMemorySseMetrics(),
        pool_stats=PoolStats(size=10, checked_out=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    lease = next(rule for rule in report.rules if rule.rule == "lease_expiry_rate")
    assert lease.triggered
    assert lease.value == 0.08
    assert report.status == STATUS_CRITICAL


def test_alert_sse_disconnect_is_warning() -> None:
    metrics = InMemorySseMetrics()
    for _ in range(20):
        asyncio.run(metrics.record("stream_started"))
    for _ in range(3):
        asyncio.run(metrics.record("client_disconnect"))
    service = AlertService(
        _FakeSession([0, 0, 0, 0, 0]),  # type: ignore[arg-type]
        metrics=metrics,
        pool_stats=PoolStats(size=10, checked_out=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    sse = next(rule for rule in report.rules if rule.rule == "sse_disconnect")
    assert sse.triggered
    assert sse.value == pytest.approx(0.15)
    assert report.status == STATUS_WARNING


def test_alert_sse_storage_degradation_fires_below_sample() -> None:
    metrics = InMemorySseMetrics()
    asyncio.run(metrics.record("stream_started"))
    asyncio.run(metrics.record("follow_error"))
    service = AlertService(
        _FakeSession([0, 0, 0, 0, 0]),  # type: ignore[arg-type]
        metrics=metrics,
        pool_stats=PoolStats(size=10, checked_out=1),
        settings=Settings(_env_file=None),
    )
    report = asyncio.run(service.evaluate(now=NOW))
    sse = next(rule for rule in report.rules if rule.rule == "sse_disconnect")
    assert sse.triggered
    assert sse.facts["storage_degraded"] is True


# ── Langfuse 外发边界 ──────────────────────────────────────────────────────────


class _FakeLangfuse:
    instances: list[_FakeLangfuse] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.traces: list[dict[str, Any]] = []
        self.scores: list[dict[str, Any]] = []
        self.flushed = False
        type(self).instances.append(self)

    def trace(self, **kwargs: Any) -> None:
        self.traces.append(kwargs)

    def score(self, **kwargs: Any) -> None:
        self.scores.append(kwargs)

    def flush(self) -> None:
        self.flushed = True


@pytest.fixture(autouse=True)
def _reset_langfuse(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeLangfuse.instances.clear()
    monkeypatch.setattr(langfuse, "Langfuse", _FakeLangfuse)
    LangfuseClient._instance = None
    LangfuseClient._client = None
    LangfuseClient._enabled = False
    yield
    LangfuseClient._instance = None
    LangfuseClient._client = None
    LangfuseClient._enabled = False


def _langfuse_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "langfuse_public_key": "pk-test",
        "langfuse_secret_key": "sk-test",
        "lock_master_key": "0123456789abcdef",
    }
    values.update(overrides)
    return Settings(**values)


def test_mask_identifier_is_stable_and_opaque() -> None:
    first = mask_identifier("user-1", salt="salt")
    assert first == mask_identifier("user-1", salt="salt")
    assert first != mask_identifier("user-2", salt="salt")
    assert first.startswith("masked_")
    assert "user-1" not in first
    assert mask_identifier(None, salt="salt") is None


def test_langfuse_external_requires_explicit_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.langfuse_service.get_settings",
        lambda: _langfuse_settings(),
    )
    client = get_langfuse()
    assert not client.enabled
    assert client.export_mode == EXPORT_DISABLED
    assert _FakeLangfuse.instances == []
    assert (
        client.create_trace(
            trace_id="trace-1",
            run_id=uuid4(),
            thread_id=uuid4(),
            user_id=None,
            agent_slug="front-desk",
        )
        is None
    )


def test_langfuse_external_masks_identifiers_and_drops_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.langfuse_service.get_settings",
        lambda: _langfuse_settings(langfuse_allow_external_export=True),
    )
    client = get_langfuse()
    assert client.enabled
    assert client.export_mode == EXPORT_MASKED_EXTERNAL
    fake = _FakeLangfuse.instances[-1]

    run_id, thread_id, user_id = uuid4(), uuid4(), uuid4()
    assert (
        client.create_trace(
            trace_id="trace-2",
            run_id=run_id,
            thread_id=thread_id,
            user_id=user_id,
            agent_slug="front-desk",
        )
        == "trace-2"
    )
    trace = fake.traces[-1]
    assert trace["user_id"].startswith("masked_")
    assert trace["session_id"].startswith("masked_")
    assert str(user_id) not in trace["user_id"]
    assert str(thread_id) not in trace["session_id"]
    assert trace["metadata"]["run_id"] == str(run_id)

    client.score_feedback(
        trace_id="trace-2",
        name="user_feedback",
        value=0.0,
        comment="客人原话：房间太吵",
    )
    assert fake.scores[-1]["comment"] is None


def test_langfuse_self_hosted_keeps_raw_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.langfuse_service.get_settings",
        lambda: _langfuse_settings(langfuse_self_hosted=True),
    )
    client = get_langfuse()
    assert client.enabled
    assert client.export_mode == EXPORT_SELF_HOSTED
    fake = _FakeLangfuse.instances[-1]

    thread_id = uuid4()
    client.create_trace(
        trace_id="trace-3",
        run_id=uuid4(),
        thread_id=thread_id,
        user_id=None,
        agent_slug="front-desk",
    )
    assert fake.traces[-1]["session_id"] == str(thread_id)
    client.score_feedback(trace_id="trace-3", name="user_feedback", value=1.0, comment="有帮助")
    assert fake.scores[-1]["comment"] == "有帮助"
