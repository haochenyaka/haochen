"""创建数据中台表：清洗规则、管道运行记录、归档数据

创建三张数据中台核心表：
- data_cleaning_rules：清洗任务配置与调度
- data_pipeline_runs：每次执行的运行记录
- data_archives：从主表迁出的历史数据

Revision ID: 20261003_0020
Revises: 20260930_0019
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261003_0020"
down_revision: str | None = "20260930_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 清洗规则表
    op.create_table(
        "data_cleaning_rules",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", sa.String(64), nullable=False, unique=True),
        sa.Column("handler_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("schedule_cron", sa.String(64), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # 管道运行记录表
    op.create_table(
        "data_pipeline_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("rule_id", sa.BigInteger(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("source_table", sa.String(64), nullable=True),
        sa.Column("processed_rows", sa.Integer(), nullable=True),
        sa.Column("affected_rows", sa.Integer(), nullable=True),
        sa.Column("error_rows", sa.Integer(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 数据归档表
    op.create_table(
        "data_archives",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_table", sa.String(64), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column(
            "archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("data_archives")
    op.drop_table("data_pipeline_runs")
    op.drop_table("data_cleaning_rules")
