"""工具审批事实：危险工具挂起等待人工审批的持久记录。

一次待审批 = 一行：run/thread/工具/打码入参摘要 + 状态（pending → approved/denied →
consumed）。审批决定经此表落库，恢复入口据此构造 `Command(resume=...)` 恢复图；
consumed 表示决定已被图消费（Run 已续跑），不再重复恢复。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

APPROVAL_STATUSES: tuple[str, ...] = ("pending", "approved", "denied", "consumed")


class ToolApprovalRequest(Base):
    """工具审批请求：pending 待决定，approved/denied 已决定，consumed 已被图消费。"""

    __tablename__ = "tool_approval_requests"
    __table_args__ = (
        Index("ix_tool_approvals_status_created_at", "status", "created_at"),
        Index("ix_tool_approvals_run_id", "run_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("agent_runs.id"), nullable=False)
    thread_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("threads.id"), nullable=False)
    call_id: Mapped[str] = mapped_column(String(128), nullable=False)
    """模型给出的工具调用 id：与图的 interrupt 一一对应，恢复时定位同一调用。"""
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    args_digest: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """打码后的入参副本（审计口径），需审批的工具默认不含明文。"""
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    decided_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
