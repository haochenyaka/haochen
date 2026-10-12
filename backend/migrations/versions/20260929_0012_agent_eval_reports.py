"""创建 agent_eval_reports 表

P8.1 智能体评估：Langfuse Dataset 驱动端到端工具链实验，报告只追加标记作废。

Revision ID: 20260929_0012
Revises: 20260929_0011
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260929_0012"
down_revision: str | None = "20260929_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_eval_reports",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_path", sa.String(256), nullable=False),
        sa.Column("dataset_fingerprint", sa.String(80), nullable=False),
        sa.Column("config_fingerprint", sa.String(80), nullable=False),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("metrics", JSONB(), nullable=False),
        sa.Column("outcomes", JSONB(), nullable=False),
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
        sa.Column("invalidated_reason", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_agent_eval_reports_dataset_created_at",
        "agent_eval_reports",
        ["dataset_fingerprint", "created_at"],
    )
    op.create_index(
        "ix_agent_eval_reports_created_at",
        "agent_eval_reports",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_eval_reports_created_at", table_name="agent_eval_reports")
    op.drop_index(
        "ix_agent_eval_reports_dataset_created_at",
        table_name="agent_eval_reports",
    )
    op.drop_table("agent_eval_reports")