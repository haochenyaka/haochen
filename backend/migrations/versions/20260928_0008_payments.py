"""P4-T2 支付事实：payments

Revision ID: 20260928_0008
Revises: 20260927_0007
Create Date: 2026-09-28

说明：微信 JSAPI 下单尝试与回调事实。回调以 transaction_id 为幂等键（唯一约束兜底
并发重复投递），重复回调命中既有成功事实即返回、不产生第二单（验收主张 A3）。
库中只存渠道单号与回调事实，不存支付凭证明文；金额比对以本表 amount_fen 为准。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0008"
down_revision: str | None = "20260927_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("openid", sa.String(length=64), nullable=False),
        sa.Column("prepay_id", sa.String(length=64), nullable=True),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="created", nullable=False),
        sa.Column("transaction_id", sa.String(length=64), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("transaction_id", name="uq_payments_transaction_id"),
    )
    op.create_index("ix_payments_order_created_at", "payments", ["order_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_payments_order_created_at", table_name="payments")
    op.drop_table("payments")
