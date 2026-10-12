"""会话反馈与转人工仓储：反馈按 (消息, 身份) 唯一，转人工请求按线程取最新。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import HANDOFF_STATUS_PENDING, HandoffRequest, MessageFeedback


class MessageFeedbackRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, *, message_id: UUID, user_id: UUID) -> MessageFeedback | None:
        stmt = select(MessageFeedback).where(
            MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def set_rating(
        self,
        *,
        message_id: UUID,
        user_id: UUID,
        rating: str,
        comment: str | None = None,
    ) -> MessageFeedback:
        """存在即改评，否则新建；并发下的双写由唯一约束兜底（调用方按冲突重试）。"""
        feedback = await self.get(message_id=message_id, user_id=user_id)
        if feedback is None:
            feedback = MessageFeedback(
                message_id=message_id, user_id=user_id, rating=rating, comment=comment
            )
            self._session.add(feedback)
            await self._session.flush()
            return feedback
        feedback.rating = rating
        feedback.comment = comment
        await self._session.flush()
        return feedback

    async def delete(self, *, message_id: UUID, user_id: UUID) -> bool:
        feedback = await self.get(message_id=message_id, user_id=user_id)
        if feedback is None:
            return False
        await self._session.delete(feedback)
        await self._session.flush()
        return True

    async def ratings_for(self, *, message_ids: Sequence[UUID], user_id: UUID) -> dict[UUID, str]:
        """批量读当前身份对一批消息的评分：消息列表页一次取回，避免逐条查询。"""
        if not message_ids:
            return {}
        stmt = select(MessageFeedback).where(
            MessageFeedback.message_id.in_(message_ids), MessageFeedback.user_id == user_id
        )
        rows = (await self._session.execute(stmt)).scalars()
        return {row.message_id: row.rating for row in rows}


class HandoffRequestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, *, thread_id: UUID, user_id: UUID, reason: str | None) -> HandoffRequest:
        request = HandoffRequest(thread_id=thread_id, user_id=user_id, reason=reason)
        self._session.add(request)
        await self._session.flush()
        return request

    async def get_latest_for_thread(self, thread_id: UUID) -> HandoffRequest | None:
        stmt = (
            select(HandoffRequest)
            .where(HandoffRequest.thread_id == thread_id)
            .order_by(HandoffRequest.created_at.desc(), HandoffRequest.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def get_pending_for_thread(self, thread_id: UUID) -> HandoffRequest | None:
        stmt = (
            select(HandoffRequest)
            .where(
                HandoffRequest.thread_id == thread_id,
                HandoffRequest.status == HANDOFF_STATUS_PENDING,
            )
            .order_by(HandoffRequest.created_at.desc(), HandoffRequest.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()
