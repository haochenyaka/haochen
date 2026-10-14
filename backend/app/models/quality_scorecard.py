"""数据质量评分卡事实表：每次评分产生一条不可变记录。

不变量：
- 评分卡是快照事实：每次计算生成新行，不改写历史；
- 五个维度独立评分并聚合为总分，发现数、已治理数与缺口数同时记录；
- 历史评分可回溯对比，作为数据质量趋势基线。
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class QualityScorecard(Base):
    __tablename__ = "quality_scorecards"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    generated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    """触发者：scheduled / manual / qa_auditor"""

    # 覆盖率：知识缺口→FAQ 草稿
    coverage_score: Mapped[float] = mapped_column(Float, nullable=False)
    coverage_gaps: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_actionable: Mapped[int] = mapped_column(Integer, nullable=False)
    coverage_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # 新鲜度：过期检测与降权
    freshness_score: Mapped[float] = mapped_column(Float, nullable=False)
    freshness_expired: Mapped[int] = mapped_column(Integer, nullable=False)
    freshness_actionable: Mapped[int] = mapped_column(Integer, nullable=False)
    freshness_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # 正确性：幻觉嫌疑与业务校验失败
    correctness_score: Mapped[float] = mapped_column(Float, nullable=False)
    correctness_issues: Mapped[int] = mapped_column(Integer, nullable=False)
    correctness_actionable: Mapped[int] = mapped_column(Integer, nullable=False)
    correctness_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # 一致性：冲突答案
    consistency_score: Mapped[float] = mapped_column(Float, nullable=False)
    consistency_conflicts: Mapped[int] = mapped_column(Integer, nullable=False)
    consistency_actionable: Mapped[int] = mapped_column(Integer, nullable=False)
    consistency_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # 召回质量：低基线 recall 知识块
    recall_quality_score: Mapped[float] = mapped_column(Float, nullable=False)
    recall_low_count: Mapped[int] = mapped_column(Integer, nullable=False)
    recall_actionable: Mapped[int] = mapped_column(Integer, nullable=False)
    recall_quality_detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)

    # 综合
    overall_score: Mapped[float] = mapped_column(Float, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)

    # 附：最近一次 KB 评估和 Agent 评估的 report_id 引用
    latest_kb_eval_report_id: Mapped[int | None] = mapped_column(BigInteger)
    latest_agent_eval_report_id: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
