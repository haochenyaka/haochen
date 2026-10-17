"""C 端会话接入路由：薄路由只做输入校验与错误映射，用例在 service。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Literal, NoReturn
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.registry import AgentRegistryError, get_agent
from app.db import SessionFactory, get_session
from app.models.runtime import User
from app.repositories import AUDIT_READ_LIMIT, UserRepository
from app.services.agent_request_queue_service import (
    AgentRequestQueueService,
    QueueConflictError,
    QueueServiceError,
    RequestNotFoundError,
    ThreadAccessError,
    ThreadNotFoundError,
)
from app.services.audit_service import AuditTimelineService
from app.services.conversation_service import (
    MESSAGE_READ_LIMIT,
    MESSAGE_READ_MAX,
    ConversationService,
    ConversationServiceError,
    FeedbackTargetError,
    MessageNotFoundError,
)
from app.services.observability_service import NullSseMetrics, SseMetrics
from app.services.run_dispatch import NullRunDispatcher, RunDispatcher, dispatch_run
from app.services.run_event_bus import NullRunEventBus, RunEventBus
from app.services.run_stream import RunAccessError, RunNotFoundError, RunStreamService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest/chat", tags=["guest-chat"])

DEV_USERNAME_HEADER = "X-Dev-Username"
DEFAULT_DEV_USERNAME = "dev-guest"

SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

_fallback_dispatcher = NullRunDispatcher()


class MessageContent(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SubmitMessageBody(BaseModel):
    content: MessageContent
    agent_slug: str = Field(default="front-desk", max_length=64)
    thread_id: UUID | None = None
    request_id: UUID | None = None
    policy: Literal["enqueue", "reject"] = "enqueue"


class SubmitMessageResponse(BaseModel):
    thread_id: UUID
    message_id: UUID
    request_id: UUID
    run_id: UUID | None
    queue_position: int | None
    deduplicated: bool


class RequestStatusResponse(BaseModel):
    request_id: UUID
    thread_id: UUID
    status: str
    queue_position: int | None
    run_id: UUID | None
    run_status: str | None
    error: str | None


class CancelResponse(BaseModel):
    request_id: UUID
    status: str


class AuditEntryResponse(BaseModel):
    kind: Literal["model", "tool"]
    id: int
    run_id: str
    seq: int
    agent_slug: str
    status: str
    created_at: datetime
    duration_ms: int | None = None
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieval_refs: list[dict[str, object]] | None = None
    tool_name: str | None = None
    args_digest: dict[str, object] | None = None
    idempotency_key: str | None = None


class AuditTimelineResponse(BaseModel):
    entries: list[AuditEntryResponse]
    truncated: bool


class MessageViewResponse(BaseModel):
    message_id: UUID
    role: str
    text: str
    run_id: str | None
    created_at: datetime
    citations: list[dict[str, object]]
    my_rating: Literal["up", "down"] | None


class MessagesResponse(BaseModel):
    messages: list[MessageViewResponse]


class FeedbackBody(BaseModel):
    rating: Literal["up", "down"] | None
    comment: str | None = Field(default=None, max_length=500)


class FeedbackResponse(BaseModel):
    message_id: UUID
    rating: Literal["up", "down"] | None


class HandoffBody(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class HandoffResponse(BaseModel):
    thread_id: UUID
    request_id: UUID | None
    status: str
    reason: str | None
    created_at: datetime | None


async def get_current_guest(
    session: Annotated[AsyncSession, Depends(get_session)],
    x_dev_username: Annotated[str, Header(alias=DEV_USERNAME_HEADER)] = DEFAULT_DEV_USERNAME,
) -> User:
    """P1 开发态身份（请求头直传用户名）；P2 起由微信登录派生。"""
    return await UserRepository(session).get_or_create(username=x_dev_username)


async def get_stream_guest(
    x_dev_username: Annotated[str, Header(alias=DEV_USERNAME_HEADER)] = DEFAULT_DEV_USERNAME,
) -> User:
    """SSE 身份：流生命周期长，身份用短会话解析后即释放，不占用请求连接。"""
    async with SessionFactory() as session:
        user = await UserRepository(session).get_or_create(username=x_dev_username)
        await session.commit()
        return user


def get_run_dispatcher(request: Request) -> RunDispatcher:
    dispatcher: RunDispatcher | None = getattr(request.app.state, "run_dispatcher", None)
    return dispatcher if dispatcher is not None else _fallback_dispatcher


def get_run_event_bus(request: Request) -> RunEventBus:
    bus: RunEventBus | None = getattr(request.app.state, "run_event_bus", None)
    return bus if bus is not None else NullRunEventBus()


_FALLBACK_SSE_METRICS = NullSseMetrics()


def get_sse_metrics(request: Request) -> SseMetrics:
    metrics: SseMetrics | None = getattr(request.app.state, "sse_metrics", None)
    return metrics if metrics is not None else _FALLBACK_SSE_METRICS


def get_run_stream_service(
    bus: Annotated[RunEventBus, Depends(get_run_event_bus)],
    metrics: Annotated[SseMetrics, Depends(get_sse_metrics)],
) -> RunStreamService:
    return RunStreamService(bus=bus, metrics=metrics)


def _raise_http(exc: QueueServiceError | ConversationServiceError) -> NoReturn:
    if isinstance(exc, (ThreadNotFoundError, RequestNotFoundError, MessageNotFoundError)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, ThreadAccessError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    if isinstance(exc, (QueueConflictError, FeedbackTargetError)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="接入用例异常")


def _ensure_chat_entry(agent_slug: str) -> None:
    """非聊天入口的 Agent（如离线质检员）不允许客人建 Run：接入侧建 Run 之前直接拒绝。

    未注册 slug 不做判定，仍交运行期按既有路径处理（不扩大本次改动的行为面）。
    """
    try:
        agent = get_agent(agent_slug)
    except AgentRegistryError:
        return
    if not agent.chat_entry:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{agent_slug} 不是聊天入口 Agent",
        )


@router.post("/messages", response_model=SubmitMessageResponse, status_code=status.HTTP_201_CREATED)
async def submit_message(
    body: SubmitMessageBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
    dispatcher: Annotated[RunDispatcher, Depends(get_run_dispatcher)],
) -> SubmitMessageResponse:
    _ensure_chat_entry(body.agent_slug)
    service = AgentRequestQueueService(session)
    try:
        submission = await service.submit_message(
            user_id=user.id,
            agent_slug=body.agent_slug,
            content={"text": body.content.text},
            thread_id=body.thread_id,
            request_id=body.request_id,
            policy=body.policy,
        )
    except QueueServiceError as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    if submission.deduplicated:
        response.status_code = status.HTTP_200_OK
    await dispatch_run(
        dispatcher, submission.dispatch.run_id if submission.dispatch is not None else None
    )
    return SubmitMessageResponse(
        thread_id=submission.thread_id,
        message_id=submission.message_id,
        request_id=submission.request_id,
        run_id=submission.run_id,
        queue_position=submission.queue_position,
        deduplicated=submission.deduplicated,
    )


@router.get("/stream")
async def stream_run_events(
    run_id: Annotated[UUID, Query()],
    user: Annotated[User, Depends(get_stream_guest)],
    service: Annotated[RunStreamService, Depends(get_run_stream_service)],
    last_event_id: Annotated[str | None, Query(max_length=64)] = None,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    """运行事件流（SSE）：回放 + 跟随 + 数据库终态兜底，Last-Event-ID 断线续传。

    客户端以提交响应中的 run_id 订阅；断线重连时回传最后一次收到的 id（标准
    Last-Event-ID 头或 last_event_id 查询参数）即可续传，缺失事件按数据库投影补齐。
    """
    try:
        view = await service.authorize(run_id=run_id, user_id=user.id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RunAccessError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return StreamingResponse(
        service.stream(run=view, last_event_id=last_event_id_header or last_event_id),
        media_type="text/event-stream",
        headers=SSE_RESPONSE_HEADERS,
    )


@router.get("/requests/{request_id}", response_model=RequestStatusResponse)
async def get_request_status(
    request_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
) -> RequestStatusResponse:
    service = AgentRequestQueueService(session)
    try:
        view = await service.describe_request(request_id=request_id, user_id=user.id)
    except QueueServiceError as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    return RequestStatusResponse(
        request_id=view.request_id,
        thread_id=view.thread_id,
        status=view.status,
        queue_position=view.queue_position,
        run_id=view.run_id,
        run_status=view.run_status,
        error=view.error,
    )


@router.post("/requests/{request_id}/cancel", response_model=CancelResponse)
async def cancel_request(
    request_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
    dispatcher: Annotated[RunDispatcher, Depends(get_run_dispatcher)],
) -> CancelResponse:
    service = AgentRequestQueueService(session)
    try:
        outcome = await service.cancel_request(request_id=request_id, user_id=user.id)
    except QueueServiceError as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    await dispatch_run(
        dispatcher, outcome.dispatch.run_id if outcome.dispatch is not None else None
    )
    return CancelResponse(request_id=outcome.request_id, status=outcome.status)


@router.get("/threads/{thread_id}/messages", response_model=MessagesResponse)
async def list_thread_messages(
    thread_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
    limit: Annotated[int, Query(ge=1, le=MESSAGE_READ_MAX)] = MESSAGE_READ_LIMIT,
) -> MessagesResponse:
    """会话页历史：最近 limit 条按时间升序返回；助手消息带引用与本人评分。

    引用取该消息所属 Run 的最后一次模型调用检索结果，历史重开后引用仍可溯源。
    """
    try:
        views = await ConversationService(session).list_messages(
            thread_id=thread_id, user_id=user.id, limit=limit
        )
    except (QueueServiceError, ConversationServiceError) as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    return MessagesResponse(messages=[MessageViewResponse(**vars(view)) for view in views])


@router.put("/messages/{message_id}/feedback", response_model=FeedbackResponse)
async def put_message_feedback(
    message_id: UUID,
    body: FeedbackBody,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
) -> FeedbackResponse:
    """点赞/点踩/取消：rating 为 null 即撤销，重复提交幂等（同一身份一条记录）。"""
    try:
        view = await ConversationService(session).set_feedback(
            message_id=message_id,
            user_id=user.id,
            rating=body.rating,
            comment=body.comment,
        )
    except (QueueServiceError, ConversationServiceError) as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    return FeedbackResponse(message_id=view.message_id, rating=view.rating)


@router.post(
    "/threads/{thread_id}/handoff",
    response_model=HandoffResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_handoff(
    thread_id: UUID,
    body: HandoffBody,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
) -> HandoffResponse:
    """转人工入口：同一线程已有待处理请求时复用（幂等），不重复建单。"""
    try:
        outcome = await ConversationService(session).request_handoff(
            thread_id=thread_id, user_id=user.id, reason=body.reason
        )
    except (QueueServiceError, ConversationServiceError) as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    if not outcome.created:
        response.status_code = status.HTTP_200_OK
    return HandoffResponse(**vars(outcome.view))


@router.get("/threads/{thread_id}/handoff", response_model=HandoffResponse)
async def get_handoff(
    thread_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
) -> HandoffResponse:
    """最近一次转人工请求；从未请求过时 status 为 none（会话页据此决定是否展示入口）。"""
    try:
        view = await ConversationService(session).describe_handoff(
            thread_id=thread_id, user_id=user.id
        )
    except (QueueServiceError, ConversationServiceError) as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    return HandoffResponse(**vars(view))


@router.get("/threads/{thread_id}/audits", response_model=AuditTimelineResponse)
async def get_thread_audits(
    thread_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    user: Annotated[User, Depends(get_current_guest)],
    limit: Annotated[int, Query(ge=1, le=AUDIT_READ_LIMIT)] = AUDIT_READ_LIMIT,
) -> AuditTimelineResponse:
    """线程级 Model/Tool 审计时间线（倒序，最新 limit 条 + truncated）。"""
    try:
        timeline = await AuditTimelineService(session).thread_timeline(
            thread_id=thread_id, user_id=user.id, limit=limit
        )
    except QueueServiceError as exc:
        await session.rollback()
        _raise_http(exc)
    await session.commit()
    return AuditTimelineResponse(
        entries=[AuditEntryResponse(**vars(entry)) for entry in timeline.entries],
        truncated=timeline.truncated,
    )
