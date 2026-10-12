"""请求接入用例：消息与请求先落库、线程 FIFO 派发、request_id 幂等与冲突。

不变量：
- ARQ 投递在数据库提交之后：本服务只产出投递计划，调用方提交后再投递；
- 只有队头 ready 请求创建 AgentRun；同一 (用户, Agent, 线程) 同时只有一个活跃 Run；
- intake 与接力都在线程行锁内读写，数据库唯一约束兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runtime import Thread
from app.repositories import (
    AgentRunRepository,
    MessageRepository,
    RequestRepository,
    ThreadRepository,
)
from app.services.thread_dispatch import DispatchPlan, promote_head_if_idle


class QueueServiceError(Exception):
    """接入用例错误基类：路由层据此映射 HTTP 语义。"""


class ThreadNotFoundError(QueueServiceError):
    pass


class RequestNotFoundError(QueueServiceError):
    pass


class ThreadAccessError(QueueServiceError):
    """线程不属于当前身份。"""


class QueueConflictError(QueueServiceError):
    """request_id 复用冲突、Agent 不匹配、线程忙或状态不可取消。"""


@dataclass(frozen=True)
class MessageSubmission:
    thread_id: UUID
    message_id: UUID
    request_id: UUID
    run_id: UUID | None
    queue_position: int | None
    deduplicated: bool
    dispatch: DispatchPlan | None


@dataclass(frozen=True)
class RequestStatus:
    request_id: UUID
    thread_id: UUID
    status: str
    queue_position: int | None
    run_id: UUID | None
    run_status: str | None
    error: str | None


@dataclass(frozen=True)
class CancelOutcome:
    request_id: UUID
    status: str
    dispatch: DispatchPlan | None


class AgentRequestQueueService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._threads = ThreadRepository(session)
        self._messages = MessageRepository(session)
        self._requests = RequestRepository(session)
        self._runs = AgentRunRepository(session)

    async def submit_message(
        self,
        *,
        user_id: UUID,
        agent_slug: str,
        content: dict[str, object],
        thread_id: UUID | None = None,
        request_id: UUID | None = None,
        policy: str = "enqueue",
    ) -> MessageSubmission:
        if policy not in ("enqueue", "reject"):
            raise QueueConflictError(f"未知队列策略：{policy}")

        if request_id is not None:
            existing = await self._requests.get_by_id(request_id)
            if existing is not None:
                return await self._reuse_existing(
                    existing_id=existing.id,
                    user_id=user_id,
                    agent_slug=agent_slug,
                    thread_id=thread_id,
                )

        thread = await self._resolve_thread(
            user_id=user_id, agent_slug=agent_slug, thread_id=thread_id
        )
        if policy == "reject" and await self._thread_has_backlog(thread.id):
            raise QueueConflictError("线程正忙：同步策略不排队，请稍后重试或改用 enqueue")

        message = await self._messages.append(thread_id=thread.id, role="user", content=content)
        request = await self._requests.create(
            user_id=user_id,
            agent_slug=agent_slug,
            thread_id=thread.id,
            message_id=message.id,
            request_id=request_id,
        )
        await self._threads.touch(thread.id)

        dispatch = await promote_head_if_idle(self._session, thread.id)
        run = await self._runs.get_by_request_id(request.id)
        return MessageSubmission(
            thread_id=thread.id,
            message_id=message.id,
            request_id=request.id,
            run_id=run.id if run is not None else None,
            queue_position=await self._requests.queued_position(request.id),
            deduplicated=False,
            dispatch=dispatch,
        )

    async def describe_request(self, *, request_id: UUID, user_id: UUID) -> RequestStatus:
        request = await self._requests.get_by_id(request_id)
        if request is None or request.user_id != user_id:
            raise RequestNotFoundError(str(request_id))
        run = await self._runs.get_by_request_id(request.id)
        return RequestStatus(
            request_id=request.id,
            thread_id=request.thread_id,
            status=request.status,
            queue_position=await self._requests.queued_position(request.id),
            run_id=run.id if run is not None else None,
            run_status=run.status if run is not None else None,
            error=run.error if run is not None else None,
        )

    async def cancel_request(self, *, request_id: UUID, user_id: UUID) -> CancelOutcome:
        request = await self._requests.get_by_id(request_id)
        if request is None or request.user_id != user_id:
            raise RequestNotFoundError(str(request_id))

        thread = await self._threads.get_for_update(request.thread_id)
        if thread is None:
            raise ThreadNotFoundError(str(request.thread_id))
        request = await self._requests.get_by_id(request_id)
        if request is None:
            raise RequestNotFoundError(str(request_id))

        if request.status == "queued":
            await self._requests.mark_cancelled(request.id)
            return CancelOutcome(request_id=request.id, status="cancelled", dispatch=None)

        if request.status == "dispatched":
            run = await self._runs.get_by_request_id(request.id)
            if run is None:
                raise QueueConflictError("请求已派发但缺少关联 Run")
            if run.status == "pending":
                await self._runs.cancel_pending(run.id)
                await self._requests.mark_converged(request.id, cancelled=True)
                dispatch = await promote_head_if_idle(self._session, request.thread_id)
                return CancelOutcome(request_id=request.id, status="cancelled", dispatch=dispatch)
            if run.status == "running":
                await self._runs.request_cancel(run.id)
                return CancelOutcome(
                    request_id=request.id, status="cancel_requested", dispatch=None
                )
            raise QueueConflictError(f"运行已处于终态或取消中，不可取消：{run.status}")

        raise QueueConflictError(f"请求当前状态不可取消：{request.status}")

    async def _reuse_existing(
        self,
        *,
        existing_id: UUID,
        user_id: UUID,
        agent_slug: str,
        thread_id: UUID | None,
    ) -> MessageSubmission:
        request = await self._requests.get_by_id(existing_id)
        if request is None:
            raise RequestNotFoundError(str(existing_id))
        if request.user_id != user_id or request.agent_slug != agent_slug:
            raise QueueConflictError("request_id 已被其他用户或 Agent 使用")
        if thread_id is not None and request.thread_id != thread_id:
            raise QueueConflictError("request_id 已被其他线程使用")
        run = await self._runs.get_by_request_id(request.id)
        return MessageSubmission(
            thread_id=request.thread_id,
            message_id=request.message_id,
            request_id=request.id,
            run_id=run.id if run is not None else None,
            queue_position=await self._requests.queued_position(request.id),
            deduplicated=True,
            dispatch=None,
        )

    async def _resolve_thread(
        self, *, user_id: UUID, agent_slug: str, thread_id: UUID | None
    ) -> Thread:
        if thread_id is None:
            return await self._threads.create(user_id=user_id, agent_slug=agent_slug)
        thread = await self._threads.get_for_update(thread_id)
        if thread is None:
            raise ThreadNotFoundError(str(thread_id))
        if thread.user_id != user_id:
            raise ThreadAccessError(str(thread_id))
        if thread.agent_slug != agent_slug:
            raise QueueConflictError(
                f"线程归属 Agent 为 {thread.agent_slug}，与请求的 {agent_slug} 不一致"
            )
        return thread

    async def _thread_has_backlog(self, thread_id: UUID) -> bool:
        if await self._runs.get_active_for_thread(thread_id) is not None:
            return True
        return await self._requests.get_head_queued(thread_id) is not None
