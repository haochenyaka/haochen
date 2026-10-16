"""创建 tool_approval_requests 表

PR-0.5b 工具审批挂起：危险工具挂起等待人工审批的事实表。一次待审批一行，
decision（approved/denied）落库，恢复入口据此 resume 图，consumed 标记已消费。

Revision ID: 20261013_0021
Revises: 20261003_0020
Create Date: 2026-10-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261013_0021"
down_revision: str | None = "20261003_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_approval_requests",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_runs.id"), nullable=False),
        sa.Column("thread_id", sa.Uuid(), sa.ForeignKey("threads.id"), nullable=False),
        sa.Column("call_id", sa.String(128), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("args_digest", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("decided_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_tool_approvals_status_created_at", "tool_approval_requests", ["status", "created_at"]
    )
    op.create_index("ix_tool_approvals_run_id", "tool_approval_requests", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_approvals_run_id", table_name="tool_approval_requests")
    op.drop_index("ix_tool_approvals_status_created_at", table_name="tool_approval_requests")
    op.drop_table("tool_approval_requests")
