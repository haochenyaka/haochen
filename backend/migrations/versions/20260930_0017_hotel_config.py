"""创建 hotel_config 表

P7.6 酒店基础数据增强：新增酒店配置单行表。

Revision ID: 20260930_0017
Revises: 20260930_0016
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0017"
down_revision: str | None = "20260930_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hotel_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("hotel_name", sa.String(128), nullable=False, server_default="皓晨酒店"),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("brand", sa.String(64), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # 插入默认行
    op.execute("INSERT INTO hotel_config (id, hotel_name) VALUES (1, '皓晨酒店')")


def downgrade() -> None:
    op.drop_table("hotel_config")
