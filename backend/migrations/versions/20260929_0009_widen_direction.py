"""修复 attendance_checkins.direction 列宽 (String(8) → String(16))

check_out 长度为 9，超出原 String(8) 限制。本迁移在 P6 打卡上线前修正，
不涉及既有数据迁移。

Revision ID: 20260929_0009
Revises: 20260928_0008
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0009"
down_revision: str | None = "20260928_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "attendance_checkins",
        "direction",
        type_=sa.String(16),
        existing_type=sa.String(8),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "attendance_checkins",
        "direction",
        type_=sa.String(8),
        existing_type=sa.String(16),
        nullable=False,
    )