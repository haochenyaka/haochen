"""创建 consent_records 表

P4.5 隐私合规：PIPL 告知同意记录。每次同意/撤回追加一行，当前状态由同一
(user_id, purpose) 的最新事件决定，保留完整取证链。

Revision ID: 20261018_0025
Revises: 20261017_0024
Create Date: 2026-10-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261018_0025"
down_revision: str | None = "20261017_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "consent_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False, server_default="api"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_consent_records_user_purpose_created",
        "consent_records",
        ["user_id", "purpose", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_consent_records_user_purpose_created", table_name="consent_records")
    op.drop_table("consent_records")
