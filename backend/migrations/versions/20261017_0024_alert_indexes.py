"""告警评估索引：活跃 Run 心跳与已结束尝试窗口

P4.3 的 `/api/admin/health/alerts` 每 30 秒轮询两类查询：活跃 Run 的心跳停滞、
15 分钟窗口内已结束的尝试。原表只有租约过期（仅 running）与 run_id 索引，
大表上按时间窗扫描会随历史增长而劣化。

Revision ID: 20261017_0024
Revises: 20261014_0023
Create Date: 2026-10-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261017_0024"
down_revision: str | None = "20261014_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_runs_active_heartbeat",
        "agent_runs",
        ["last_heartbeat_at"],
        postgresql_where=sa.text("status IN ('running', 'cancel_requested')"),
    )
    op.create_index(
        "ix_agent_run_attempts_finished_started",
        "agent_run_attempts",
        ["started_at", "finished_at"],
        postgresql_where=sa.text("finished_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_attempts_finished_started", table_name="agent_run_attempts")
    op.drop_index("ix_agent_runs_active_heartbeat", table_name="agent_runs")
