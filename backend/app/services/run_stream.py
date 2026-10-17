"""SSE 状态投影：事件回放 + 实时跟随 + 数据库终态兜底。

流是投影而非事实来源：

- 订阅时按 Last-Event-ID 回放 Redis Stream 中积压的事件（断线续传），随后阻塞跟随新事件；
- 每轮跟随之后复核数据库 Run 状态：数据库已达终态而事件缺失（Redis 降级、租约被
  reconciler 收敛、订阅晚于运行结束）时，按数据库合成 message.completed 与终态事件后收流；
- 心跳注释保活；订阅前先做 Run 归属校验（不存在 404、越权 403）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from itertools import count
from time import monotonic
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db import SessionFactory
from app.models.runtime import Message
from app.repositories import (
    TERMINAL_RUN_STATUSES,
    AgentRunRepository,
    MessageRepository,
)
from app.services.observability_service import (
    SSE_CLIENT_DISCONNECT,
    SSE_FOLLOW_ERROR,
    SSE_READ_ERROR,
    SSE_STREAM_COMPLETED,
    SSE_STREAM_ERROR,
    SSE_STREAM_STARTED,
    NullSseMetrics,
    SseMetricName,
    SseMetrics,
)
from app.services.run_event_bus import RunEventBus, RunEventRecord, is_stream_id
from app.services.run_events import (
    EVENT_MESSAGE_COMPLETED,
    EVENT_RUN_FAILED,
    TERMINAL_EVENT_TYPES,
    format_sse_event,
    terminal_event_type,
)

logger = logging.getLogger(__name__)

SSE_HEARTBEAT_COMMENT = ": ping\n\n"


class RunStreamError(Exception):
    """运行事件流订阅失败。"""


class RunNotFoundError(RunStreamError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"运行不存在：{run_id}")


class RunAccessError(RunStreamError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"无权访问该运行的运行事件：{run_id}")


@dataclass(frozen=True)
class RunStreamView:
    """订阅视图：归属校验通过后的运行快照。"""

    run_id: UUID
    thread_id: UUID
    agent_slug: str
    status: str
    error: str | None


class RunStreamService:
    """事件流用例：归属校验 + SSE 生成器；会话按需短开，不占用请求连接。"""

    def __init__(
        self,
        *,
        bus: RunEventBus,
        session_factory: async_sessionmaker[AsyncSession] = SessionFactory,
        heartbeat_seconds: float | None = None,
        follow_block_ms: int | None = None,
        metrics: SseMetrics | None = None,
    ) -> None:
        settings = get_settings()
        self._bus = bus
        self._metrics = metrics or NullSseMetrics()
        self._session_factory = session_factory
        self._heartbeat_seconds = (
            settings.sse_heartbeat_seconds if heartbeat_seconds is None else heartbeat_seconds
        )
        self._follow_block_ms = (
            settings.sse_follow_block_ms if follow_block_ms is None else follow_block_ms
        )

    async def authorize(self, *, run_id: UUID, user_id: UUID) -> RunStreamView:
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get_by_id(run_id)
            if run is None:
                raise RunNotFoundError(str(run_id))
            if run.user_id != user_id:
                raise RunAccessError(str(run_id))
            return RunStreamView(
                run_id=run.id,
                thread_id=run.thread_id,
                agent_slug=run.agent_slug,
                status=run.status,
                error=run.error,
            )

    async def stream(
        self, *, run: RunStreamView, last_event_id: str | None = None
    ) -> AsyncIterator[str]:
        """SSE 事件生成器：回放 → 跟随 → 终态收流；客户端断开由框架取消生成器。"""
        await self._record(SSE_STREAM_STARTED)
        completed = False
        try:
            cursor = last_event_id if is_stream_id(last_event_id) else None
            saw_reply = False
            projected = count(1)

            for record in await self._safe_read(run.run_id, after=cursor):
                cursor = record.id
                saw_reply = saw_reply or record.event == EVENT_MESSAGE_COMPLETED
                yield format_sse_event(record)
                if record.event in TERMINAL_EVENT_TYPES:
                    completed = True
                    return

            if run.status in TERMINAL_RUN_STATUSES:
                for record in await self._project_terminal(run, saw_reply=saw_reply, ids=projected):
                    yield format_sse_event(record)
                completed = True
                return

            heartbeat_due = monotonic() + self._heartbeat_seconds
            while True:
                records = await self._safe_follow(
                    run.run_id, after=cursor, block_ms=self._follow_block_ms
                )
                for record in records:
                    cursor = record.id
                    saw_reply = saw_reply or record.event == EVENT_MESSAGE_COMPLETED
                    yield format_sse_event(record)
                    if record.event in TERMINAL_EVENT_TYPES:
                        completed = True
                        return

                view = await self._load_view(run.run_id)
                if view is None:
                    yield format_sse_event(
                        _projected_record(
                            run,
                            next(projected),
                            EVENT_RUN_FAILED,
                            {"status": "failed", "error": "运行不存在"},
                        )
                    )
                    completed = True
                    return
                if view.status in TERMINAL_RUN_STATUSES:
                    for record in await self._project_terminal(
                        view, saw_reply=saw_reply, ids=projected
                    ):
                        yield format_sse_event(record)
                    completed = True
                    return

                now = monotonic()
                if now >= heartbeat_due:
                    yield SSE_HEARTBEAT_COMMENT
                    heartbeat_due = now + self._heartbeat_seconds
        except asyncio.CancelledError:
            # 客户端断开由框架取消任务：指标写入用 shield 防止被同一次取消吞掉
            await asyncio.shield(self._record(SSE_CLIENT_DISCONNECT))
            raise
        except Exception:
            await asyncio.shield(self._record(SSE_STREAM_ERROR))
            raise
        finally:
            if completed:
                await asyncio.shield(self._record(SSE_STREAM_COMPLETED))

    async def _record(self, metric: SseMetricName) -> None:
        try:
            await self._metrics.record(metric)
        except Exception:
            logger.debug("SSE 指标写入失败（不阻断流）：%s", metric, exc_info=True)

    async def _safe_read(self, run_id: UUID, *, after: str | None) -> list[RunEventRecord]:
        try:
            return await self._bus.read(run_id, after=after)
        except Exception:
            logger.warning("run event read failed; falling back to db projection: %s", run_id)
            await self._record(SSE_READ_ERROR)
            return []

    async def _safe_follow(
        self, run_id: UUID, *, after: str | None, block_ms: int
    ) -> list[RunEventRecord]:
        try:
            return await self._bus.follow(run_id, after=after, block_ms=block_ms)
        except Exception:
            # Redis 故障：按跟随周期放慢轮询，数据库终态兜底仍然生效
            logger.warning("run event follow failed; falling back to db projection: %s", run_id)
            await self._record(SSE_FOLLOW_ERROR)
            await asyncio.sleep(block_ms / 1000)
            return []

    async def _load_view(self, run_id: UUID) -> RunStreamView | None:
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get_by_id(run_id)
            if run is None:
                return None
            return RunStreamView(
                run_id=run.id,
                thread_id=run.thread_id,
                agent_slug=run.agent_slug,
                status=run.status,
                error=run.error,
            )

    async def _project_terminal(
        self, run: RunStreamView, *, saw_reply: bool, ids: count
    ) -> list[RunEventRecord]:
        """按数据库合成终态投影：补 message.completed（事件缺失时）后给终态事件。"""
        records: list[RunEventRecord] = []
        if not saw_reply:
            reply = await self._load_reply(run.run_id)
            if reply is not None:
                records.append(
                    _projected_record(
                        run,
                        next(ids),
                        EVENT_MESSAGE_COMPLETED,
                        {"text": message_text(reply), "message_id": str(reply.id)},
                    )
                )
        event_type = terminal_event_type(run.status)
        if event_type is None:
            return records
        records.append(
            _projected_record(
                run, next(ids), event_type, {"status": run.status, "error": run.error}
            )
        )
        return records

    async def _load_reply(self, run_id: UUID) -> Message | None:
        async with self._session_factory() as session:
            return await MessageRepository(session).get_reply_for_run(run_id)


def _projected_record(
    run: RunStreamView, seq: int, event: str, data: Mapping[str, object]
) -> RunEventRecord:
    """合成事件：id 带 projected 前缀（不是传输游标，不参与续传定位）。

    合成事件都是 Run 级事件（回复与终态），故 agent_slug 与 entry_slug 同为 Run 的入口 Agent。
    """
    payload = {
        "run_id": str(run.run_id),
        "thread_id": str(run.thread_id),
        "agent_slug": run.agent_slug,
        "entry_slug": run.agent_slug,
        "projected": True,
        **data,
    }
    return RunEventRecord(id=f"projected-{seq}", event=event, data=dict(payload))


def message_text(message: Message) -> str:
    """消息正文投影：content 约定为 {"text": ...}，其余结构按空文本处理。"""
    content = message.content
    if not isinstance(content, dict):
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""
