"""消息仓储：消息先于请求落库，内容的唯一事实来源。"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import Message


class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(
        self,
        *,
        thread_id: UUID,
        role: str,
        content: dict[str, object],
        run_id: str | None = None,
        meta: dict[str, object] | None = None,
    ) -> Message:
        message = Message(thread_id=thread_id, role=role, content=content, run_id=run_id, meta=meta)
        self._session.add(message)
        await self._session.flush()
        return message

    async def get_by_id(self, message_id: UUID) -> Message | None:
        return await self._session.get(Message, message_id)

    async def list_for_thread(self, thread_id: UUID, *, limit: int = 200) -> list[Message]:
        stmt = (
            select(Message).where(Message.thread_id == thread_id).order_by(Message.seq).limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def recent_for_thread(self, thread_id: UUID, *, limit: int) -> tuple[list[Message], bool]:
        """最近 limit 条消息（仍按 seq 升序）与是否发生截断。

        多取一条用于判断是否还有更早消息，避免额外 count 查询。
        """
        if limit < 1:
            raise ValueError("limit 至少为 1")
        stmt = (
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.seq.desc())
            .limit(limit + 1)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        truncated = len(rows) > limit
        rows = rows[:limit]
        rows.reverse()
        return rows, truncated

    async def latest_for_thread(self, thread_id: UUID, *, limit: int = 50) -> list[Message]:
        """最近 limit 条消息，仍按 seq 升序返回（会话页首屏：优先看最新，顺序照旧）。"""
        stmt = (
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.seq.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars())
        rows.reverse()
        return rows

    async def get_reply_for_run(self, run_id: UUID) -> Message | None:
        """某次 Run 的最终回复：SSE 状态投影在事件缺失时按数据库补齐内容。"""
        stmt = (
            select(Message)
            .where(Message.run_id == str(run_id), Message.role == "assistant")
            .order_by(Message.seq.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()
