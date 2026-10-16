"""housekeeping_tasks 补保洁客评列

在线反馈管道（FeedbackCollector.get_staff_ratings）读取 housekeeping_tasks.guest_score /
guest_comment，但列从未落地——接口一经真实调用即报错。本迁移补齐评分事实列，并开放
客人侧评分写入路径（POST /api/guest/services/clean/{task_id}/rating）。

Revision ID: 20261013_0022
Revises: 20261013_0021
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0022"
down_revision: str | None = "20261013_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("housekeeping_tasks", sa.Column("guest_score", sa.Integer(), nullable=True))
    op.add_column("housekeeping_tasks", sa.Column("guest_comment", sa.Text(), nullable=True))
    op.add_column(
        "housekeeping_tasks",
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("housekeeping_tasks", "rated_at")
    op.drop_column("housekeeping_tasks", "guest_comment")
    op.drop_column("housekeeping_tasks", "guest_score")
