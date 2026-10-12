"""智能体评估事实表：报告只追加不修改，可比性由指纹判定。

不变量：
- 报告是不可变事实：配置变化不改写历史，只在新报告落库时把旧报告标记 superseded_by；
- 可比性三要素：dataset_fingerprint（评估集）、config_fingerprint（评估参数与模型）、
  model（被评估模型），任一不同即不可直接比较；
- outcomes 保留逐题明细（含工具链比对），供人工复核。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentEvalReport(Base):
    __tablename__ = "agent_eval_reports"
    __table_args__ = (
        Index("ix_agent_eval_reports_dataset_created_at", "dataset_fingerprint", "created_at"),
        Index("ix_agent_eval_reports_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_path: Mapped[str] = mapped_column(String(256), nullable=False)
    dataset_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    total_cases: Mapped[int] = mapped_column(Integer, nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    outcomes: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    superseded_by: Mapped[int | None] = mapped_column(BigInteger)
    invalidated_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )