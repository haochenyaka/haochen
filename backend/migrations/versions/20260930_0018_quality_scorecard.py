"""创建 quality_scorecards 表

P8.3 数据质量评分卡事实表：五个维度评分 + 抽样详情 + 治理缺口。

Revision ID: 20260930_0018
Revises: 20260930_0017
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0018"
down_revision: str | None = "20260930_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_scorecards",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("generated_by", sa.String(64), nullable=False),
        # 覆盖率
        sa.Column("coverage_score", sa.Float(), nullable=False),
        sa.Column("coverage_gaps", sa.Integer(), nullable=False),
        sa.Column("coverage_actionable", sa.Integer(), nullable=False),
        sa.Column("coverage_detail", sa.JSON(), nullable=True),
        # 新鲜度
        sa.Column("freshness_score", sa.Float(), nullable=False),
        sa.Column("freshness_expired", sa.Integer(), nullable=False),
        sa.Column("freshness_actionable", sa.Integer(), nullable=False),
        sa.Column("freshness_detail", sa.JSON(), nullable=True),
        # 正确性
        sa.Column("correctness_score", sa.Float(), nullable=False),
        sa.Column("correctness_issues", sa.Integer(), nullable=False),
        sa.Column("correctness_actionable", sa.Integer(), nullable=False),
        sa.Column("correctness_detail", sa.JSON(), nullable=True),
        # 一致性
        sa.Column("consistency_score", sa.Float(), nullable=False),
        sa.Column("consistency_conflicts", sa.Integer(), nullable=False),
        sa.Column("consistency_actionable", sa.Integer(), nullable=False),
        sa.Column("consistency_detail", sa.JSON(), nullable=True),
        # 召回质量
        sa.Column("recall_quality_score", sa.Float(), nullable=False),
        sa.Column("recall_low_count", sa.Integer(), nullable=False),
        sa.Column("recall_actionable", sa.Integer(), nullable=False),
        sa.Column("recall_quality_detail", sa.JSON(), nullable=True),
        # 综合
        sa.Column("overall_score", sa.Float(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        # 评估报告引用
        sa.Column("latest_kb_eval_report_id", sa.BigInteger(), nullable=True),
        sa.Column("latest_agent_eval_report_id", sa.BigInteger(), nullable=True),
        # 时间
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("quality_scorecards")
