"""add key_version to lock_credentials

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260930_0019"
down_revision: Union[str, None] = "20260930_0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "lock_credentials",
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("'1'")),
    )


def downgrade() -> None:
    op.drop_column("lock_credentials", "key_version")