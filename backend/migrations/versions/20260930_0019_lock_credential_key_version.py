"""add key_version to lock_credentials

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0019"
down_revision: str | None = "20260930_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lock_credentials",
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
    )


def downgrade() -> None:
    op.drop_column("lock_credentials", "key_version")
