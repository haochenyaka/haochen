"""知识库评估事实表：报告只追加不修改，可比性由三个指纹判定，作废旧基线以标记表达。

不变量：
- 报告是不可变事实：内容或配置变化不改写历史指标，只在新报告落库时把旧报告标记
  superseded_by 与 invalidated_reason；复核以 dataset_path 指向的评估集为准；
- 可比性三要素：dataset_fingerprint（评估集）、content_fingerprint（文档 checksum 与
  分块数）、config_fingerprint（检索参数与评判模型），任一不同即不可直接比较；
- outcomes 保留逐题明细（含 chunk_id 与段序），供人工复核与后续分块级黄金标注升级。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KbEvalReport(Base):
    __tablename__ = "kb_eval_reports"
    __table_args__ = (
        Index("ix_kb_eval_reports_dataset_created_at", "dataset_fingerprint", "created_at"),
        Index("ix_kb_eval_reports_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    dataset_path: Mapped[str] = mapped_column(String(256), nullable=False)
    dataset_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    config_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    k: Mapped[int] = mapped_column(Integer, nullable=False)
    questions: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluator: Mapped[str | None] = mapped_column(String(128))
    retrieval_config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    metrics: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    outcomes: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    superseded_by: Mapped[int | None] = mapped_column(BigInteger)
    invalidated_reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
