"""审计基线：model_audit 与 tool_audit

Revision ID: 20260927_0001
Revises:
Create Date: 2026-09-27

说明：Agent 运行所需的 checkpoint 表由运行时 checkpointer 的 setup 流程创建（P1 接入），
不在本迁移中定义；本迁移只建立审计事实表。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent_slug", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieval_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state_reconciled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_model_audit_run_seq"),
    )
    op.create_index("ix_model_audit_run_id", "model_audit", ["run_id"])
    op.create_index("ix_model_audit_actor_created_at", "model_audit", ["actor_id", "created_at"])

    op.create_table(
        "tool_audit",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("agent_slug", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=64), nullable=False),
        sa.Column("args_digest", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_tool_audit_run_seq"),
    )
    op.create_index("ix_tool_audit_run_id", "tool_audit", ["run_id"])
    op.create_index("ix_tool_audit_tool_created_at", "tool_audit", ["tool_name", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_audit_tool_created_at", table_name="tool_audit")
    op.drop_index("ix_tool_audit_run_id", table_name="tool_audit")
    op.drop_table("tool_audit")
    op.drop_index("ix_model_audit_actor_created_at", table_name="model_audit")
    op.drop_index("ix_model_audit_run_id", table_name="model_audit")
    op.drop_table("model_audit")
