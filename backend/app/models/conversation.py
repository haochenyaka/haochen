"""C 端会话事实：消息级在线反馈（点赞点踩）与转人工请求。

反馈先落 PostgreSQL 业务表（外部观测组件只作排障视图，审计事实以库为准），评分只有
up/down 两档，同一身份的同一消息唯一；数据治理（P8）按消息维度导出评估集。
转人工请求是宾客侧入口的事实：待处理请求由管理后台人工工作台（P7.8）接单推进，
本层只保证「客人请求过」可查且不重复建单（同线程已有待处理请求时复用）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

Rating = Literal["up", "down"]

RATING_UP: Rating = "up"
RATING_DOWN: Rating = "down"
RATINGS: frozenset[str] = frozenset({RATING_UP, RATING_DOWN})

HANDOFF_STATUS_PENDING = "pending"
"""待处理：管理后台人工工作台（P7.8）接单后推进为 accepted / closed。"""


class MessageFeedback(Base):
    """消息级反馈：一个身份对一条消息至多一条记录，改评即更新，取消即删除。"""

    __tablename__ = "message_feedback"
    __table_args__ = (
        UniqueConstraint("message_id", "user_id", name="uq_message_feedback_message_user"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("messages.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    rating: Mapped[str] = mapped_column(String(8), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HandoffRequest(Base):
    """转人工请求：线程维度的接线请求事实，重复请求复用待处理单。"""

    __tablename__ = "handoff_requests"
    __table_args__ = (Index("ix_handoff_requests_thread_created_at", "thread_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    thread_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("threads.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=HANDOFF_STATUS_PENDING
    )
    assigned_to: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    """接单运营人员（P7.8 人工工作台设置）。"""
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    """运营人员附言/备注。"""
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
