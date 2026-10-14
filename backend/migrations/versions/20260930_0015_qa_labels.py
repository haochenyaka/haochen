"""创建 qa_labels 表

会话质检标签表：qa-auditor 自动标注或人工添加，支持每个线程多个标签。

Revision ID: 20260930_0015
Revises: 20260930_0014
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260930_0015"
down_revision: str | None = "20260930_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "qa_labels",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "thread_id",
            sa.Uuid(),
            sa.ForeignKey("threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("evidence", JSONB(), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qa_labels_thread_label", "qa_labels", ["thread_id", "label"])
    op.create_index("ix_qa_labels_label_created", "qa_labels", ["label", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_qa_labels_label_created", table_name="qa_labels")
    op.drop_index("ix_qa_labels_thread_label", table_name="qa_labels")
    op.drop_table("qa_labels")
