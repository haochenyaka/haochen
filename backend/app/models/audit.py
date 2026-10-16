from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ModelAudit(Base):
    """模型调用审计事实：run 内 seq 严格递增，终态对账以 state_reconciled 证明。"""

    __tablename__ = "model_audit"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_model_audit_run_seq"),
        Index("ix_model_audit_actor_created_at", "actor_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    retrieval_refs: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    state_reconciled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RouteAudit(Base):
    """路由决策审计事实：supervisor 每次决策的判定值、判定来源与最终派发目标。

    `decided_route` 是决策原值，`dispatched_route` 是授权感知改派后的实际派发目标（收尾或
    无可派发时为 NULL）；两者不一致即说明发生过改派。
    """

    __tablename__ = "route_audit"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_route_audit_run_seq"),
        Index("ix_route_audit_agent_created_at", "agent_slug", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_route: Mapped[str] = mapped_column(String(32), nullable=False)
    dispatched_route: Mapped[str | None] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ToolAudit(Base):
    """工具调用审计事实：args_digest 为打码后的入参，密码类字段不落明文。"""

    __tablename__ = "tool_audit"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_tool_audit_run_seq"),
        Index("ix_tool_audit_tool_created_at", "tool_name", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    args_digest: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
