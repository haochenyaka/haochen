"""增强 handoff_requests: 接单运营人员、附言、关单时间

P7.8 人工工作台：添加 assigned_to/assigned_at/closed_at/note 字段。

Revision ID: 20260930_0016
Revises: 20260930_0015
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "20260930_0016"
down_revision: str | None = "20260930_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("handoff_requests", sa.Column("assigned_to", UUID(), sa.ForeignKey("users.id"), nullable=True))
    op.add_column("handoff_requests", sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("handoff_requests", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("handoff_requests", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("handoff_requests", "closed_at")
    op.drop_column("handoff_requests", "note")
    op.drop_column("handoff_requests", "assigned_at")
    op.drop_column("handoff_requests", "assigned_to")