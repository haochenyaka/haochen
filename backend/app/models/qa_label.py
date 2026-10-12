from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QALabel(Base):
    """会话质检标签：qa-auditor 自动标注或人工添加。

    每个标签独立记录，支持同一线程多个标签。source 区分标注来源。
    """

    __tablename__ = "qa_labels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(32), nullable=False)
    """标签名：resolved / unresolved / handoff_requested / complaint_risk / hallucination_suspected / other"""
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    """标注来源：auto（qa-auditor 自动）或 manual（人工标注）"""
    confidence: Mapped[float | None] = mapped_column()
    """auto 来源的可信度 0-1；manual 来源为 null"""
    comment: Mapped[str | None] = mapped_column(Text)
    """标注说明或备注"""
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """auto 来源的证据引用（如审计条目 ID、消息位置等）"""
    created_by: Mapped[str | None] = mapped_column(String(64))
    """标注人：auto 时为 'qa-auditor'，manual 时为管理员 username"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_qa_labels_thread_label", "thread_id", "label"),
        Index("ix_qa_labels_label_created", "label", "created_at"),
    )