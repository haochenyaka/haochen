"""运行事件总线：Redis Stream 承载每 Run 的事件流，不可用时降级为空实现。

事件流只做投影与断线续传：键按 Run 隔离并带 TTL 与长度上限，终态事实以 PostgreSQL
为准；发布失败只告警，不影响 Run 执行与事实落库；Redis 不可用时退回数据库状态投影。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from redis.asyncio import Redis

from app.config import get_settings

logger = logging.getLogger(__name__)

EVENT_KEY_PREFIX = "run:events:"
READ_BATCH_SIZE = 256
FOLLOW_BATCH_SIZE = 64
STREAM_ID_PATTERN = re.compile(r"^\d+-\d+$")


def run_event_key(run_id: UUID) -> str:
    return f"{EVENT_KEY_PREFIX}{run_id}"


def is_stream_id(value: str | None) -> bool:
    """Last-Event-ID 只有传输游标形态（<ms>-<seq>）时才可用于续传定位。"""
    return bool(value) and STREAM_ID_PATTERN.match(value or "") is not None


@dataclass(frozen=True)
class RunEventRecord:
    """一条已落流的事件：id 为传输游标，供 Last-Event-ID 续传。"""

    id: str
    event: str
    data: dict[str, Any]


class RunEventBus(Protocol):
    """事件总线契约：发布、回放读取、阻塞跟随；读取异常由调用方降级处理。"""

    async def publish(self, run_id: UUID, event: str, data: Mapping[str, Any]) -> str | None: ...

    async def read(
        self, run_id: UUID, *, after: str | None = None, count: int = READ_BATCH_SIZE
    ) -> list[RunEventRecord]: ...

    async def follow(
        self,
        run_id: UUID,
        *,
        after: str | None = None,
        count: int = FOLLOW_BATCH_SIZE,
        block_ms: int = 2000,
    ) -> list[RunEventRecord]: ...

    async def aclose(self) -> None: ...


class RedisRunEventBus:
    """Redis Stream 实现：XADD 发布（长度上限 + TTL 刷新），XRANGE/XREAD 读取。"""

    def __init__(self, client: Redis, *, ttl_seconds: int, maxlen: int) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._maxlen = maxlen

    async def publish(self, run_id: UUID, event: str, data: Mapping[str, Any]) -> str | None:
        key = run_event_key(run_id)
        record_id = await self._client.xadd(
            key,
            {"event": event, "data": json.dumps(data, ensure_ascii=False, default=str)},
            maxlen=self._maxlen,
            approximate=True,
        )
        await self._client.expire(key, self._ttl_seconds)
        return record_id if isinstance(record_id, str) else record_id.decode()

    async def read(
        self, run_id: UUID, *, after: str | None = None, count: int = READ_BATCH_SIZE
    ) -> list[RunEventRecord]:
        entries = await self._client.xrange(
            run_event_key(run_id), min=_after_min(after), max="+", count=count
        )
        return _records(entries)

    async def follow(
        self,
        run_id: UUID,
        *,
        after: str | None = None,
        count: int = FOLLOW_BATCH_SIZE,
        block_ms: int = 2000,
    ) -> list[RunEventRecord]:
        start = "0"
        if after is not None and is_stream_id(after):
            start = after
        result = await self._client.xread(
            {run_event_key(run_id): start}, count=count, block=block_ms
        )
        if not result:
            return []
        return _records(result[0][1])

    async def aclose(self) -> None:
        await self._client.aclose()


class NullRunEventBus:
    """Redis 不可用时的降级实现：不承载事件，SSE 侧退回数据库状态投影。"""

    async def publish(self, run_id: UUID, event: str, data: Mapping[str, Any]) -> None:
        return None

    async def read(
        self, run_id: UUID, *, after: str | None = None, count: int = READ_BATCH_SIZE
    ) -> list[RunEventRecord]:
        return []

    async def follow(
        self,
        run_id: UUID,
        *,
        after: str | None = None,
        count: int = FOLLOW_BATCH_SIZE,
        block_ms: int = 2000,
    ) -> list[RunEventRecord]:
        await asyncio.sleep(block_ms / 1000)  # 保持消费节奏，避免上层热循环
        return []

    async def aclose(self) -> None:
        return None


EventClientFactory = Callable[[], Redis]


def _default_client_factory() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def create_run_event_bus(client_factory: EventClientFactory | None = None) -> RunEventBus:
    """进程级总线：Redis 不可达时返回空实现（不阻断进程启动）。"""
    settings = get_settings()
    try:
        client = (client_factory or _default_client_factory)()
        await client.ping()
    except Exception as exc:
        logger.warning("运行事件总线不可用（SSE 退回数据库状态投影）：%s", exc)
        return NullRunEventBus()
    return RedisRunEventBus(
        client, ttl_seconds=settings.run_event_ttl_seconds, maxlen=settings.run_event_maxlen
    )


def _after_min(after: str | None) -> str:
    return f"({after}" if is_stream_id(after) else "-"


def _records(entries: Sequence[Any]) -> list[RunEventRecord]:
    records: list[RunEventRecord] = []
    for raw_id, fields in entries:
        record_id = _text(raw_id)
        raw_data = _text(fields.get("data"))
        try:
            parsed = json.loads(raw_data) if raw_data else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw_data}
        records.append(
            RunEventRecord(
                id=record_id,
                event=_text(fields.get("event")),
                data=parsed if isinstance(parsed, dict) else {"value": parsed},
            )
        )
    return records


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return "" if value is None else str(value)
