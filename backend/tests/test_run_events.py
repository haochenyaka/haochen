"""运行事件协议验证：SSE 线格式、事件包络、发布容错与总线降级。"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from app.services.run_event_bus import (
    EVENT_KEY_PREFIX,
    NullRunEventBus,
    RedisRunEventBus,
    RunEventRecord,
    create_run_event_bus,
    is_stream_id,
    run_event_key,
)
from app.services.run_events import (
    EVENT_MESSAGE_DELTA,
    EVENT_RUN_CANCELLED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_RUN_STARTED,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    TERMINAL_EVENT_TYPES,
    RunEventEmitter,
    format_sse_event,
    terminal_event_type,
)

RUN_ID = UUID("11111111-1111-1111-1111-111111111111")
THREAD_ID = UUID("22222222-2222-2222-2222-222222222222")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class RecordingBus:
    """事件总线替身：记录发布事件。"""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    async def publish(self, run_id: UUID, event: str, data: dict[str, Any]) -> str:
        self.published.append({"run_id": run_id, "event": event, "data": data})
        return f"{len(self.published)}-0"

    async def read(self, *args: Any, **kwargs: Any) -> list[RunEventRecord]:
        return []

    async def follow(self, *args: Any, **kwargs: Any) -> list[RunEventRecord]:
        return []

    async def aclose(self) -> None:
        return None


def test_format_sse_event_wire_format() -> None:
    record = RunEventRecord(
        id="7-1",
        event=EVENT_MESSAGE_DELTA,
        data={"run_id": str(RUN_ID), "text": "你好"},
    )
    text = format_sse_event(record)
    assert text.startswith("id: 7-1\nevent: message.delta\ndata: ")
    assert text.endswith("\n\n")
    payload = json.loads(text.split("data: ", 1)[1].strip())
    assert payload == {"run_id": str(RUN_ID), "text": "你好"}
    assert "\\u" not in text  # 中文不转义（ensure_ascii=False）


def test_terminal_event_type_mapping() -> None:
    assert terminal_event_type("completed") == EVENT_RUN_COMPLETED
    assert terminal_event_type("failed") == EVENT_RUN_FAILED
    assert terminal_event_type("cancelled") == EVENT_RUN_CANCELLED
    assert terminal_event_type("running") is None
    assert terminal_event_type("interrupted") is None
    assert TERMINAL_EVENT_TYPES == {EVENT_RUN_COMPLETED, EVENT_RUN_FAILED, EVENT_RUN_CANCELLED}


def test_emitter_envelope_seq_and_protocol_methods() -> None:
    bus = RecordingBus()
    emitter = RunEventEmitter(run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk", bus=bus)

    async def _emit() -> None:
        await emitter.run_started()
        await emitter.message_delta("你好")
        await emitter.tool_call(tool="get_current_time", call_id="c1", args={})
        await emitter.tool_result(
            tool="get_current_time", call_id="c1", status="completed", duration_ms=3
        )
        await emitter.message_completed(text="回复", message_id=RUN_ID)
        await emitter.interrupt(tool="rotate_credential", call_id="c2", reason="需要审批")
        await emitter.run_finished(status="completed", token_usage={"model_calls": 1})

    run(_emit())

    events = [item["event"] for item in bus.published]
    assert events == [
        EVENT_RUN_STARTED,
        EVENT_MESSAGE_DELTA,
        EVENT_TOOL_CALL,
        EVENT_TOOL_RESULT,
        "message.completed",
        "interrupt",
        EVENT_RUN_COMPLETED,
    ]
    for index, item in enumerate(bus.published, start=1):
        assert item["run_id"] == RUN_ID
        assert item["data"]["run_id"] == str(RUN_ID)
        assert item["data"]["thread_id"] == str(THREAD_ID)
        assert item["data"]["agent_slug"] == "front-desk"
        assert item["data"]["seq"] == index
        assert "projected" not in item["data"]

    tail = bus.published[-1]["data"]
    assert tail["status"] == "completed"
    assert tail["token_usage"] == {"model_calls": 1}
    assert emitter.seq == len(bus.published)


def test_emitter_truncates_error_and_maps_failure_status() -> None:
    bus = RecordingBus()
    emitter = RunEventEmitter(run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk", bus=bus)
    run(emitter.run_finished(status="failed", error="长" * 900, error_class="RuntimeError"))
    record = bus.published[0]
    assert record["event"] == EVENT_RUN_FAILED
    assert len(record["data"]["error"]) == 500
    assert record["data"]["error_class"] == "RuntimeError"

    run(emitter.run_finished(status="cancelled"))
    assert bus.published[1]["event"] == EVENT_RUN_CANCELLED


def test_first_token_hook_fires_once_and_swallows_failure() -> None:
    calls: list[int] = []

    async def _hook() -> None:
        calls.append(1)

    bus = RecordingBus()
    emitter = RunEventEmitter(
        run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk", bus=bus, on_first_token=_hook
    )

    async def _emit() -> None:
        await emitter.message_delta("你")
        await emitter.message_delta("好")
        await emitter.message_completed(text="你好")

    run(_emit())
    assert len(calls) == 1  # 仅首个增量触发一次
    deltas = [item for item in bus.published if item["event"] == EVENT_MESSAGE_DELTA]
    assert len(deltas) == 2

    async def _boom() -> None:
        raise RuntimeError("db down")

    exploding = RunEventEmitter(
        run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk", bus=bus, on_first_token=_boom
    )
    run(exploding.message_delta("x"))  # 埋点失败不影响增量投影
    assert exploding.seq == 1


def test_emitter_swallows_publish_failure_and_no_bus_is_noop() -> None:
    class ExplodingBus(RecordingBus):
        async def publish(self, run_id: UUID, event: str, data: dict[str, Any]) -> str:
            raise ConnectionError("redis down")

    run(
        RunEventEmitter(
            run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk", bus=ExplodingBus()
        ).run_started()
    )
    silent = RunEventEmitter(run_id=RUN_ID, thread_id=THREAD_ID, agent_slug="front-desk")
    run(silent.message_delta("任意"))
    assert silent.seq == 1


class FakeRedisClient:
    """Redis 客户端替身：记录调用并返回预置流数据。"""

    def __init__(
        self,
        *,
        xadd_result: str = "9-0",
        xrange_result: list[Any] | None = None,
        xread_result: list[Any] | None = None,
        ping_error: Exception | None = None,
    ) -> None:
        self.xadd_result = xadd_result
        self.xrange_result = xrange_result or []
        self.xread_result = xread_result or []
        self.ping_error = ping_error
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def ping(self) -> bool:
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def xadd(self, key: str, fields: dict[str, Any], **kwargs: Any) -> str:
        self.calls.append(("xadd", (key, fields), kwargs))
        return self.xadd_result

    async def expire(self, key: str, ttl: int) -> bool:
        self.calls.append(("expire", (key, ttl), {}))
        return True

    async def xrange(self, key: str, **kwargs: Any) -> list[Any]:
        self.calls.append(("xrange", (key,), kwargs))
        return self.xrange_result

    async def xread(self, streams: dict[str, str], **kwargs: Any) -> list[Any]:
        self.calls.append(("xread", (streams,), kwargs))
        return self.xread_result

    async def aclose(self) -> None:
        self.calls.append(("aclose", (), {}))


def test_redis_bus_publish_sets_key_maxlen_and_ttl() -> None:
    client = FakeRedisClient()
    bus = RedisRunEventBus(client, ttl_seconds=3600, maxlen=512)  # type: ignore[arg-type]
    record_id = run(bus.publish(RUN_ID, EVENT_RUN_STARTED, {"seq": 1}))

    assert record_id == "9-0"
    kind, args, kwargs = client.calls[0]
    assert kind == "xadd"
    assert args[0] == f"{EVENT_KEY_PREFIX}{RUN_ID}"
    assert args[1] == {"event": EVENT_RUN_STARTED, "data": json.dumps({"seq": 1})}
    assert kwargs == {"maxlen": 512, "approximate": True}
    assert client.calls[1] == ("expire", (f"{EVENT_KEY_PREFIX}{RUN_ID}", 3600), {})


def test_redis_bus_read_uses_exclusive_cursor_and_decodes_records() -> None:
    client = FakeRedisClient(
        xrange_result=[
            ["3-0", {"event": "run.started", "data": json.dumps({"seq": 1})}],
            ["4-0", {"event": "message.delta", "data": "not-json"}],
        ]
    )
    bus = RedisRunEventBus(client, ttl_seconds=60, maxlen=16)  # type: ignore[arg-type]

    records = run(bus.read(RUN_ID, after="3-0", count=128))
    kind, args, kwargs = client.calls[0]
    assert kind == "xrange"
    assert kwargs == {"min": "(3-0", "max": "+", "count": 128}
    assert [record.id for record in records] == ["3-0", "4-0"]
    assert records[0].data == {"seq": 1}
    assert records[1].data == {"raw": "not-json"}

    run(bus.read(RUN_ID, after="projected-1"))
    assert client.calls[1][2]["min"] == "-"  # 非游标形态从头回放

    client.xread_result = [["k", [["5-0", {"event": "run.completed", "data": "{}"}]]]]
    followed = run(bus.follow(RUN_ID, after="4-0", count=8, block_ms=50))
    assert client.calls[2] == (
        "xread",
        ({f"{EVENT_KEY_PREFIX}{RUN_ID}": "4-0"},),
        {"count": 8, "block": 50},
    )
    assert [record.event for record in followed] == ["run.completed"]


def test_redis_bus_follow_without_cursor_reads_from_zero(monkeypatch: Any) -> None:
    client = FakeRedisClient(xread_result=[])
    bus = RedisRunEventBus(client, ttl_seconds=60, maxlen=16)  # type: ignore[arg-type]
    assert run(bus.follow(RUN_ID)) == []
    assert client.calls[0][1][0] == {f"{EVENT_KEY_PREFIX}{RUN_ID}": "0"}


def test_is_stream_id_accepts_only_transport_cursor() -> None:
    assert is_stream_id("1700000000000-0") is True
    assert is_stream_id("projected-1") is False
    assert is_stream_id("") is False
    assert is_stream_id(None) is False


def test_null_bus_is_silent_and_paces_follow() -> None:
    bus = NullRunEventBus()

    async def _exercise() -> tuple[Any, float]:
        import time

        assert await bus.publish(RUN_ID, EVENT_RUN_STARTED, {}) is None
        assert await bus.read(RUN_ID) == []
        started = time.monotonic()
        records = await bus.follow(RUN_ID, block_ms=30)
        return records, time.monotonic() - started

    records, elapsed = run(_exercise())
    assert records == []
    assert elapsed >= 0.03


def test_create_run_event_bus_falls_back_when_redis_unavailable() -> None:
    bus = run(
        create_run_event_bus(
            client_factory=lambda: FakeRedisClient(ping_error=ConnectionError("down"))
        )
    )  # type: ignore[arg-type,return-value]
    assert isinstance(bus, NullRunEventBus)

    healthy = run(create_run_event_bus(client_factory=lambda: FakeRedisClient()))  # type: ignore[arg-type,return-value]
    assert isinstance(healthy, RedisRunEventBus)


def test_run_event_key_is_per_run() -> None:
    other = uuid4()
    assert run_event_key(RUN_ID) != run_event_key(other)
    assert run_event_key(RUN_ID).startswith(EVENT_KEY_PREFIX)
