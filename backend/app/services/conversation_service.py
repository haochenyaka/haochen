"""会话读取与反馈用例：历史消息（含引用）、点赞点踩、转人工。

口径：
- 消息引用取该 Run 最后一次模型调用的检索引用；引用只读展示，不复制到消息行；
- 反馈落本地业务表（message_feedback），外部可观测平台只作排障视图；
- 转人工请求按线程幂等：已有 pending 即复用，不产生重复工单。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import RATING_DOWN, RATING_UP, RATINGS, HandoffRequest, Rating
from app.models.runtime import Message
from app.repositories import (
    AuditRepository,
    HandoffRequestRepository,
    MessageFeedbackRepository,
    MessageRepository,
    ThreadRepository,
)
from app.services.agent_request_queue_service import ThreadAccessError, ThreadNotFoundError
from app.services.run_stream import message_text

MESSAGE_READ_LIMIT = 50
MESSAGE_READ_MAX = 200


class ConversationServiceError(Exception):
    """会话用例错误基类：路由层据此映射 HTTP 语义。"""


class MessageNotFoundError(ConversationServiceError):
    """消息不存在或不属于当前身份。"""


class FeedbackTargetError(ConversationServiceError):
    """评分的对象不是助手回复（评分只针对可被溯源的答复）。"""


@dataclass(frozen=True)
class MessageView:
    message_id: UUID
    role: str
    text: str
    run_id: str | None
    created_at: datetime
    citations: list[dict[str, object]]
    my_rating: Rating | None


@dataclass(frozen=True)
class FeedbackView:
    message_id: UUID
    rating: Rating | None


@dataclass(frozen=True)
class HandoffView:
    thread_id: UUID
    status: str
    request_id: UUID | None
    reason: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class HandoffOutcome:
    view: HandoffView
    created: bool


class ConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._threads = ThreadRepository(session)
        self._messages = MessageRepository(session)
        self._feedback = MessageFeedbackRepository(session)
        self._handoffs = HandoffRequestRepository(session)
        self._audits = AuditRepository(session)

    async def list_messages(
        self, *, thread_id: UUID, user_id: UUID, limit: int = MESSAGE_READ_LIMIT
    ) -> list[MessageView]:
        await self._require_thread(thread_id=thread_id, user_id=user_id)
        messages = await self._messages.latest_for_thread(thread_id, limit=limit)
        ratings = await self._feedback.ratings_for(
            message_ids=[message.id for message in messages], user_id=user_id
        )
        refs = await self._citations_for(messages)
        return [
            MessageView(
                message_id=message.id,
                role=message.role,
                text=message_text(message),
                run_id=message.run_id,
                created_at=message.created_at,
                citations=refs.get(message.run_id or "", []),
                my_rating=_rating_value(ratings.get(message.id)),
            )
            for message in messages
        ]

    async def set_feedback(
        self,
        *,
        message_id: UUID,
        user_id: UUID,
        rating: Rating | None,
        comment: str | None = None,
    ) -> FeedbackView:
        if rating is not None and rating not in RATINGS:
            raise FeedbackTargetError(f"未知评分取值：{rating}")
        message = await self._require_owned_message(message_id=message_id, user_id=user_id)
        if rating is None:
            await self._feedback.delete(message_id=message.id, user_id=user_id)
            return FeedbackView(message_id=message.id, rating=None)
        if message.role != "assistant":
            raise FeedbackTargetError("只有助手回复可以评分")
        await self._feedback.set_rating(
            message_id=message.id, user_id=user_id, rating=rating, comment=comment
        )
        return FeedbackView(message_id=message.id, rating=rating)

    async def request_handoff(
        self, *, thread_id: UUID, user_id: UUID, reason: str | None = None
    ) -> HandoffOutcome:
        await self._require_thread(thread_id=thread_id, user_id=user_id)
        pending = await self._handoffs.get_pending_for_thread(thread_id)
        if pending is not None:
            return HandoffOutcome(view=_handoff_view(thread_id, pending), created=False)
        created = await self._handoffs.create(thread_id=thread_id, user_id=user_id, reason=reason)
        return HandoffOutcome(view=_handoff_view(thread_id, created), created=True)

    async def describe_handoff(self, *, thread_id: UUID, user_id: UUID) -> HandoffView:
        await self._require_thread(thread_id=thread_id, user_id=user_id)
        latest = await self._handoffs.get_latest_for_thread(thread_id)
        return _handoff_view(thread_id, latest)

    async def _require_thread(self, *, thread_id: UUID, user_id: UUID) -> None:
        thread = await self._threads.get_by_id(thread_id)
        if thread is None:
            raise ThreadNotFoundError(str(thread_id))
        if thread.user_id != user_id:
            raise ThreadAccessError(str(thread_id))

    async def _require_owned_message(self, *, message_id: UUID, user_id: UUID) -> Message:
        message = await self._messages.get_by_id(message_id)
        if message is None:
            raise MessageNotFoundError(str(message_id))
        thread = await self._threads.get_by_id(message.thread_id)
        if thread is None or thread.user_id != user_id:
            raise MessageNotFoundError(str(message_id))
        return message

    async def _citations_for(self, messages: list[Message]) -> dict[str, list[dict[str, object]]]:
        run_ids: list[str] = []
        for message in messages:
            if message.role == "assistant" and message.run_id:
                run_ids.append(message.run_id)
        if not run_ids:
            return {}
        return await self._audits.retrieval_refs_by_run(run_ids)


def _rating_value(value: str | None) -> Rating | None:
    """库中读出的评分收敛到两档枚举：未知取值按未评分处理（写入侧由字面量类型约束）。"""
    if value == RATING_UP:
        return RATING_UP
    if value == RATING_DOWN:
        return RATING_DOWN
    return None


def _handoff_view(thread_id: UUID, request: HandoffRequest | None) -> HandoffView:
    if request is None:
        return HandoffView(
            thread_id=thread_id,
            status="none",
            request_id=None,
            reason=None,
            created_at=None,
        )
    return HandoffView(
        thread_id=thread_id,
        status=request.status,
        request_id=request.id,
        reason=request.reason,
        created_at=request.created_at,
    )


__all__ = [
    "MESSAGE_READ_LIMIT",
    "MESSAGE_READ_MAX",
    "ConversationService",
    "ConversationServiceError",
    "FeedbackTargetError",
    "FeedbackView",
    "HandoffOutcome",
    "HandoffView",
    "MessageNotFoundError",
    "MessageView",
]
