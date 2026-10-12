"""管理端会话监控服务：会话列表/详情、QA 标签、转人工管理 + 人工工作台回复。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import and_, func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import HANDOFF_STATUS_PENDING, HandoffRequest
from app.models.qa_label import QALabel
from app.models.runtime import Message, Thread, User

MESSAGE_READ_LIMIT = 200


@dataclass
class ConversationSummary:
    thread_id: str
    title: str | None
    agent_slug: str
    username: str
    user_display_name: str | None
    message_count: int
    handoff_status: str | None
    labels: list[str]
    last_message_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class MessageView:
    message_id: str
    seq: int
    role: str
    text: str
    run_id: str | None
    created_at: datetime
    meta: dict[str, object] | None


@dataclass
class ThreadDetail:
    thread_id: str
    title: str | None
    agent_slug: str
    username: str
    user_display_name: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[MessageView]
    labels: list[dict[str, object]]


@dataclass
class HandoffSummary:
    id: str
    thread_id: str
    username: str
    reason: str | None
    status: str
    assigned_to_username: str | None
    assigned_at: datetime | None
    note: str | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminConversationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_conversations(
        self,
        *,
        agent_slug: str | None = None,
        status: str | None = None,
        label: str | None = None,
        search: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[ConversationSummary], int]:
        """查询会话列表，返回 (items, total)。"""
        msg_count = (
            select(Message.thread_id, sa_func.count(Message.id).label("cnt"))
            .group_by(Message.thread_id)
            .subquery()
        )
        handoff_sub = (
            select(
                HandoffRequest.thread_id,
                HandoffRequest.status.label("handoff_status"),
            )
            .distinct(HandoffRequest.thread_id)
            .order_by(HandoffRequest.thread_id, HandoffRequest.created_at.desc())
            .subquery()
        )
        last_msg = (
            select(Message.thread_id, sa_func.max(Message.created_at).label("last_at"))
            .group_by(Message.thread_id)
            .subquery()
        )

        base = (
            select(
                Thread,
                User.username,
                User.display_name,
                sa_func.coalesce(msg_count.c.cnt, 0).label("msg_count"),
                handoff_sub.c.handoff_status,
                last_msg.c.last_at,
            )
            .join(User, Thread.user_id == User.id)
            .outerjoin(msg_count, Thread.id == msg_count.c.thread_id)
            .outerjoin(handoff_sub, Thread.id == handoff_sub.c.thread_id)
            .outerjoin(last_msg, Thread.id == last_msg.c.thread_id)
        )

        conditions = []
        if agent_slug:
            conditions.append(Thread.agent_slug == agent_slug)
        if status == "handoff_pending":
            conditions.append(handoff_sub.c.handoff_status == HANDOFF_STATUS_PENDING)
        if search:
            conditions.append(User.username.ilike(f"%{search}%"))
        if label:
            label_exists = (
                select(QALabel.id)
                .where(
                    QALabel.thread_id == Thread.id,
                    QALabel.label == label,
                )
                .exists()
            )
            base = base.where(label_exists)

        if conditions:
            base = base.where(and_(*conditions))

        # Count
        count_q = select(sa_func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_q)).scalar() or 0

        # Fetch page
        query = (
            base.order_by(last_msg.c.last_at.desc().nulls_last(), Thread.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(query)).all()

        # Batch load labels for these threads
        thread_ids = [row.Thread.id for row in rows]
        labels_map: dict[UUID, list[str]] = {}
        if thread_ids:
            label_q = select(QALabel.thread_id, QALabel.label).where(
                QALabel.thread_id.in_(thread_ids)
            )
            label_rows = (await self._session.execute(label_q)).all()
            for lr in label_rows:
                labels_map.setdefault(lr.thread_id, []).append(lr.label)

        items = [
            ConversationSummary(
                thread_id=str(row.Thread.id),
                title=row.Thread.title,
                agent_slug=row.Thread.agent_slug,
                username=row.username,
                user_display_name=row.display_name,
                message_count=row.msg_count or 0,
                handoff_status=row.handoff_status,
                labels=labels_map.get(row.Thread.id, []),
                last_message_at=row.last_at,
                created_at=row.Thread.created_at,
                updated_at=row.Thread.updated_at,
            )
            for row in rows
        ]
        return items, total

    async def get_thread_detail(self, thread_id: UUID) -> ThreadDetail | None:
        stmt = (
            select(Thread, User.username, User.display_name)
            .join(User, Thread.user_id == User.id)
            .where(Thread.id == thread_id)
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        thread = row.Thread

        msg_q = (
            select(Message)
            .where(Message.thread_id == thread_id)
            .order_by(Message.seq)
            .limit(MESSAGE_READ_LIMIT)
        )
        msg_rows = (await self._session.execute(msg_q)).scalars().all()
        messages = [
            MessageView(
                message_id=str(msg.id),
                seq=msg.seq,
                role=msg.role,
                text=(msg.content.get("text") or "") if msg.content else "",
                run_id=msg.run_id,
                created_at=msg.created_at,
                meta=msg.meta,
            )
            for msg in msg_rows
        ]

        label_q = (
            select(QALabel)
            .where(QALabel.thread_id == thread_id)
            .order_by(QALabel.created_at.desc())
        )
        label_rows = (await self._session.execute(label_q)).scalars().all()
        labels = [
            {
                "id": lb.id,
                "label": lb.label,
                "source": lb.source,
                "confidence": lb.confidence,
                "comment": lb.comment,
                "created_by": lb.created_by,
                "created_at": lb.created_at.isoformat(),
            }
            for lb in label_rows
        ]

        return ThreadDetail(
            thread_id=str(thread.id),
            title=thread.title,
            agent_slug=thread.agent_slug,
            username=row.username,
            user_display_name=row.display_name,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            messages=messages,
            labels=labels,
        )

    async def set_label(
        self,
        *,
        thread_id: UUID,
        label: str,
        source: str = "manual",
        comment: str | None = None,
        created_by: str,
    ) -> QALabel:
        qa = QALabel(
            thread_id=thread_id,
            label=label,
            source=source,
            comment=comment,
            created_by=created_by,
        )
        self._session.add(qa)
        await self._session.commit()
        await self._session.refresh(qa)
        return qa

    async def remove_label(self, label_id: int) -> bool:
        stmt = select(QALabel).where(QALabel.id == label_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def list_handoffs(
        self,
        *,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[HandoffSummary], int]:
        base = (
            select(
                HandoffRequest,
                User.username,
                User.display_name,
            )
            .join(User, HandoffRequest.user_id == User.id)
        )
        # 左连接 assigned_to 以获取接单运营人员用户名
        assigned_user = User.__table__.alias("assigned_user")
        base = base.outerjoin(
            assigned_user,
            HandoffRequest.assigned_to == assigned_user.c.id,
        )

        if status:
            base = base.where(HandoffRequest.status == status)

        count_q = select(sa_func.count()).select_from(base.subquery())
        total = (await self._session.execute(count_q)).scalar() or 0

        query = (
            base.order_by(HandoffRequest.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = (await self._session.execute(query)).all()
        items = [
            HandoffSummary(
                id=str(row.HandoffRequest.id),
                thread_id=str(row.HandoffRequest.thread_id),
                username=row.username,
                reason=row.HandoffRequest.reason,
                status=row.HandoffRequest.status,
                assigned_to_username=getattr(row, "username_1", None) or getattr(row, "username", None),
                assigned_at=row.HandoffRequest.assigned_at,
                note=row.HandoffRequest.note,
                closed_at=row.HandoffRequest.closed_at,
                created_at=row.HandoffRequest.created_at,
                updated_at=row.HandoffRequest.updated_at,
            )
            for row in rows
        ]
        return items, total

    async def accept_handoff(self, handoff_id: UUID, assigned_to: UUID, assigned_to_username: str) -> bool:
        stmt = select(HandoffRequest).where(HandoffRequest.id == handoff_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        now = datetime.now(timezone.utc)
        row.status = "accepted"
        row.assigned_to = assigned_to
        row.assigned_at = now
        await self._session.commit()
        return True

    async def close_handoff(self, handoff_id: UUID, note: str | None = None) -> bool:
        stmt = select(HandoffRequest).where(HandoffRequest.id == handoff_id)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return False
        now = datetime.now(timezone.utc)
        row.status = "closed"
        row.closed_at = now
        if note is not None:
            row.note = note
        await self._session.commit()
        return True

    async def send_operator_reply(
        self,
        *,
        thread_id: UUID,
        operator_user_id: UUID,
        operator_username: str,
        text: str,
    ) -> MessageView | None:
        """运营人员发送人工回复到会话线程。"""
        stmt = select(Thread).where(Thread.id == thread_id)
        thread = (await self._session.execute(stmt)).scalar_one_or_none()
        if thread is None:
            return None

        # 获取下一个 seq
        seq_q = (
            select(sa_func.coalesce(sa_func.max(Message.seq), 0) + 1)
            .where(Message.thread_id == thread_id)
        )
        next_seq = (await self._session.execute(seq_q)).scalar() or 1

        msg = Message(
            thread_id=thread_id,
            seq=next_seq,
            role="assistant",
            content={"text": text},
            meta={
                "operator": True,
                "operator_user_id": str(operator_user_id),
                "operator_username": operator_username,
            },
        )
        self._session.add(msg)
        await self._session.commit()
        await self._session.refresh(msg)
        return MessageView(
            message_id=str(msg.id),
            seq=msg.seq,
            role=msg.role,
            text=text,
            run_id=None,
            created_at=msg.created_at,
            meta=msg.meta,
        )

    async def has_active_handoff(self, thread_id: UUID) -> bool:
        """检查线程是否有已接单（进行中）的转人工请求。"""
        stmt = select(HandoffRequest).where(
            HandoffRequest.thread_id == thread_id,
            HandoffRequest.status == "accepted",
        ).limit(1)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return row is not None

    async def get_thread_by_handoff(self, handoff_id: UUID) -> tuple[UUID | None, str | None]:
        """获取 handoff 对应的 thread_id 和状态。"""
        stmt = select(HandoffRequest.thread_id, HandoffRequest.status).where(
            HandoffRequest.id == handoff_id
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None, None
        return row.thread_id, row.status