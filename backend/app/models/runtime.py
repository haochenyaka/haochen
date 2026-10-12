from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """接入身份：P1 为开发态身份，P2 起管理端为账号密码，P4/P5 接入微信与员工登录。"""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("username", name="uq_users_username"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="customer")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Thread(Base):
    """会话线程：请求 FIFO 与运行归属的最小隔离单元。"""

    __tablename__ = "threads"
    __table_args__ = (
        Index("ix_threads_user_agent_created_at", "user_id", "agent_slug", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Message(Base):
    """线程消息事实：先于请求落库，内容的唯一事实来源；seq 为插入顺序键。"""

    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_thread_created_at", "thread_id", "created_at"),
        Index("ix_messages_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("threads.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict[str, object] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Request(Base):
    """接入请求：request_id 全局唯一（幂等键）；seq 为 FIFO 队列顺序键。"""

    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_thread_status_created_at", "thread_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("threads.id"), nullable=False)
    message_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("messages.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentRun(Base):
    """Agent 运行：租约驱动，无跨进程互斥假设，过期即显式失败。"""

    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_agent_runs_request_id"),
        Index("ix_agent_runs_status_created_at", "status", "created_at"),
        Index("ix_agent_runs_thread_created_at", "thread_id", "created_at"),
        Index(
            "ix_agent_runs_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("requests.id"), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("threads.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    owner_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_token_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_usage: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    langfuse_trace_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AgentRunAttempt(Base):
    """运行尝试事实：每次认领一条，不可变，用于诊断崩溃与租约过期。"""

    __tablename__ = "agent_run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_no", name="uq_agent_run_attempts_run_attempt"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_token: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(32))
    error_class: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
