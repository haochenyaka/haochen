"""知识库评估基线：kb_eval_reports 报告事实表

Revision ID: 20260927_0006
Revises: 20260927_0005
Create Date: 2026-09-27

说明：P3-T6 评估基线落库。报告只追加不修改；dataset/content/config 三个指纹判定
可比性，内容或配置变化时旧报告被标记 superseded_by 与 invalidated_reason，历史
指标原样保留。outcomes 为逐题明细（JSONB），含引用片段与指标。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0006"
down_revision: str | None = "20260927_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kb_eval_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_path", sa.String(length=256), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("config_fingerprint", sa.String(length=80), nullable=False),
        sa.Column("k", sa.Integer(), nullable=False),
        sa.Column("questions", sa.Integer(), nullable=False),
        sa.Column("evaluator", sa.String(length=128), nullable=True),
        sa.Column("retrieval_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("metrics", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("outcomes", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
        sa.Column("invalidated_reason", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_kb_eval_reports_dataset_created_at",
        "kb_eval_reports",
        ["dataset_fingerprint", "created_at"],
    )
    op.create_index("ix_kb_eval_reports_created_at", "kb_eval_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_kb_eval_reports_created_at", table_name="kb_eval_reports")
    op.drop_index("ix_kb_eval_reports_dataset_created_at", table_name="kb_eval_reports")
    op.drop_table("kb_eval_reports")
