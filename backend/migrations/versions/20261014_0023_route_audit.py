"""新增 route_audit：路由决策审计事实

supervisor 每次决策在派发前留下一条事实：判定值（decided_route）、判定来源（llm/keyword）
与最终派发目标（dispatched_route，授权感知改派后可能与判定值不同）。此前决策结果只以图的
`goto` 目标存在，Run 结束后无从复盘派发依据。

Revision ID: 20261014_0023
Revises: 20261013_0022
Create Date: 2026-10-14

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0023"
down_revision: str | None = "20261013_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "route_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent_slug", sa.String(length=64), nullable=False),
        sa.Column("decided_route", sa.String(length=32), nullable=False),
        sa.Column("dispatched_route", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_route_audit_run_seq"),
    )
    op.create_index("ix_route_audit_run_id", "route_audit", ["run_id"])
    op.create_index("ix_route_audit_agent_created_at", "route_audit", ["agent_slug", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_route_audit_agent_created_at", table_name="route_audit")
    op.drop_index("ix_route_audit_run_id", table_name="route_audit")
    op.drop_table("route_audit")
