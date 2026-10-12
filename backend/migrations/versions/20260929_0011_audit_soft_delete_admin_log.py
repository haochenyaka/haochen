"""model_audit / tool_audit 增加 deleted_at；创建 admin_action_log 表

P7.5 智能体日志批量删除以软删 + 管理操作留痕实现。软删行默认不出现在日志列表，
但报表与健康面板仍按全量审计事实（含软删行）重算（验收主张 A4）。

Revision ID: 20260929_0011
Revises: 20260929_0010
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0011"
down_revision: str | None = "20260929_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_audit",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tool_audit",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "admin_action_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("actor_username", sa.String(64), nullable=False),
        sa.Column("actor_role", sa.String(16), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_kind", sa.String(32), nullable=False),
        sa.Column("target_ids", sa.JSON(), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_admin_action_log_action"), "admin_action_log", ["action"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_action_log_action"), table_name="admin_action_log")
    op.drop_table("admin_action_log")
    op.drop_column("tool_audit", "deleted_at")
    op.drop_column("model_audit", "deleted_at")