"""数据管道运行记录增加 task_type 列

背景（PR-D1）：运行记录此前只存 task_id，管理端列表无法直接显示管道类型；同时导出
管道的 checkpoint 读取键与写入键不一致（用 task_type 查 source_table），导致增量失效。
本迁移加 task_type 列，供展示与 checkpoint scope 对齐。

Revision ID: 20261018_0026
Revises: 20261018_0025
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261018_0026"
down_revision: str | None = "20261018_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_pipeline_runs",
        sa.Column("task_type", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_pipeline_runs", "task_type")