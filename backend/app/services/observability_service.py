"""可观测性事实采集：SSE 流生命周期计数与连接池状态。

SSE 计数写 Redis 分钟桶：API 多副本聚合到同一窗口，键带 TTL 自动回收；Redis 不可用时
降级为进程内分钟桶（仍可本地排障，但不再跨副本聚合）。计数只用于告警评估，不是业务
事实来源，写入失败一律不阻断流。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from redis.asyncio import Redis

from app.config import get_settings
from app.db import engine

logger = logging.getLogger(__name__)

SSE_METRIC_KEY_PREFIX = "metrics:sse:"
SSE_METRIC_TTL_FLOOR_SECONDS = 60

SseMetricName = Literal[
    "stream_started",
    "stream_completed",
    "client_disconnect",
    "stream_error",
    "follow_error",
    "read_error",
]
SSE_METRIC_NAMES: tuple[SseMetricName, ...] = (
    "stream_started",
    "stream_completed",
    "client_disconnect",
    "stream_error",
    "follow_error",
    "read_error",
)

SSE_STREAM_STARTED: SseMetricName = "stream_started"
SSE_STREAM_COMPLETED: SseMetricName = "stream_completed"
SSE_CLIENT_DISCONNECT: SseMetricName = "client_disconnect"
SSE_STREAM_ERROR: SseMetricName = "stream_error"
SSE_FOLLOW_ERROR: SseMetricName = "follow_error"
SSE_READ_ERROR: SseMetricName = "read_error"


def sse_metric_key(minute: int) -> str:
    return f"{SSE_METRIC_KEY_PREFIX}{minute}"


@dataclass(frozen=True)
class SseMetricsSnapshot:
    window_minutes: int
    started: int = 0
    completed: int = 0
    client_disconnects: int = 0
    stream_errors: int = 0
    follow_errors: int = 0
    read_errors: int = 0

    @property
    def abnormal(self) -> int:
        """异常终止：每个订阅至多计一次（客户端断开 / 服务端异常）。"""
        return self.client_disconnects + self.stream_errors

    @property
    def storage_errors(self) -> int:
        """事件总线失败按跟随周期累加，不参与订阅比率，只用于降级判定。"""
        return self.follow_errors + self.read_errors

    @property
    def disconnect_rate(self) -> float | None:
        if self.started <= 0:
            return None
        return self.abnormal / self.started


@dataclass(frozen=True)
class PoolStats:
    size: int
    checked_out: int
    overflow: int = 0

    @property
    def utilization(self) -> float | None:
        if self.size <= 0:
            return None
        return self.checked_out / self.size


class SseMetrics(Protocol):
    async def record(self, metric: SseMetricName, amount: int = 1) -> None: ...

    async def snapshot(self, *, window_minutes: int) -> SseMetricsSnapshot: ...

    async def aclose(self) -> None: ...


class NullSseMetrics:
    """未注入指标实现时的空实现：只保证调用点零分支。"""

    async def record(self, metric: SseMetricName, amount: int = 1) -> None:
        return None

    async def snapshot(self, *, window_minutes: int) -> SseMetricsSnapshot:
        return SseMetricsSnapshot(window_minutes=window_minutes)

    async def aclose(self) -> None:
        return None


class InMemorySseMetrics:
    """进程内分钟桶：Redis 不可用时的降级实现，按 TTL 剪除过期桶。"""

    def __init__(
        self,
        *,
        ttl_seconds: int = 3600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl_seconds = max(ttl_seconds, SSE_METRIC_TTL_FLOOR_SECONDS)
        self._clock = clock
        self._buckets: dict[int, dict[str, int]] = {}

    def _minute(self) -> int:
        return int(self._clock() // 60)

    def _prune(self) -> None:
        oldest = self._minute() - self._ttl_seconds // 60
        for minute in [minute for minute in self._buckets if minute < oldest]:
            del self._buckets[minute]

    async def record(self, metric: SseMetricName, amount: int = 1) -> None:
        self._prune()
        bucket = self._buckets.setdefault(self._minute(), {})
        bucket[metric] = bucket.get(metric, 0) + amount

    async def snapshot(self, *, window_minutes: int) -> SseMetricsSnapshot:
        current = self._minute()
        totals: dict[str, int] = {name: 0 for name in SSE_METRIC_NAMES}
        for minute in range(current - max(window_minutes, 1) + 1, current + 1):
            for name, value in self._buckets.get(minute, {}).items():
                if name in totals:
                    totals[name] += value
        return SseMetricsSnapshot(
            window_minutes=window_minutes,
            started=totals["stream_started"],
            completed=totals["stream_completed"],
            client_disconnects=totals["client_disconnect"],
            stream_errors=totals["stream_error"],
            follow_errors=totals["follow_error"],
            read_errors=totals["read_error"],
        )

    async def aclose(self) -> None:
        self._buckets.clear()


class RedisSseMetrics:
    """Redis 分钟桶：HINCRBY 计数 + TTL；快照按窗口求和。"""

    def __init__(self, client: Redis, *, ttl_seconds: int) -> None:
        self._client = client
        self._ttl_seconds = max(ttl_seconds, SSE_METRIC_TTL_FLOOR_SECONDS)

    async def record(self, metric: SseMetricName, amount: int = 1) -> None:
        if metric not in SSE_METRIC_NAMES:
            return
        key = sse_metric_key(int(time.time() // 60))
        try:
            async with self._client.pipeline(transaction=False) as pipe:
                pipe.hincrby(key, metric, amount)
                pipe.expire(key, self._ttl_seconds)
                await pipe.execute()
        except Exception as exc:
            logger.debug("SSE 指标写入失败（不阻断流）：%s", exc)

    async def snapshot(self, *, window_minutes: int) -> SseMetricsSnapshot:
        current = int(time.time() // 60)
        totals: dict[str, int] = {name: 0 for name in SSE_METRIC_NAMES}
        try:
            async with self._client.pipeline(transaction=False) as pipe:
                for minute in range(current - max(window_minutes, 1) + 1, current + 1):
                    pipe.hgetall(sse_metric_key(minute))
                rows = await pipe.execute()
        except Exception as exc:
            logger.warning("SSE 指标读取失败，按零值报告：%s", exc)
            return SseMetricsSnapshot(window_minutes=window_minutes)
        for row in rows:
            for name, value in row.items():
                key = name.decode() if isinstance(name, bytes) else str(name)
                if key not in totals:
                    continue
                try:
                    totals[key] += int(value)
                except (TypeError, ValueError):
                    continue
        return SseMetricsSnapshot(
            window_minutes=window_minutes,
            started=totals["stream_started"],
            completed=totals["stream_completed"],
            client_disconnects=totals["client_disconnect"],
            stream_errors=totals["stream_error"],
            follow_errors=totals["follow_error"],
            read_errors=totals["read_error"],
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def _default_client_factory() -> Redis:
    return Redis.from_url(get_settings().redis_url, decode_responses=True)


async def create_sse_metrics(client_factory: Callable[[], Redis] | None = None) -> SseMetrics:
    """进程级指标实现：Redis 可用则跨副本聚合，否则降级为进程内计数。"""
    settings = get_settings()
    try:
        client = (client_factory or _default_client_factory)()
        await client.ping()
    except Exception as exc:
        logger.warning("SSE 指标存储不可用，降级为进程内计数：%s", exc)
        return InMemorySseMetrics(ttl_seconds=settings.sse_metrics_ttl_seconds)
    return RedisSseMetrics(client, ttl_seconds=settings.sse_metrics_ttl_seconds)


def database_pool_stats() -> PoolStats | None:
    """本进程 SQLAlchemy 连接池状态；池实现不支持统计时返回 None。"""
    pool = engine.pool
    # 基类 Pool 没有 size/checkedout/overflow，队列池才提供；缺项的池按不可统计处理
    size = getattr(pool, "size", None)
    checked_out = getattr(pool, "checkedout", None)
    overflow = getattr(pool, "overflow", None)
    if not (callable(size) and callable(checked_out) and callable(overflow)):
        return None
    try:
        return PoolStats(
            size=int(size()),
            checked_out=int(checked_out()),
            overflow=int(overflow()),
        )
    except Exception as exc:
        logger.debug("连接池统计不可用：%s", exc)
        return None
