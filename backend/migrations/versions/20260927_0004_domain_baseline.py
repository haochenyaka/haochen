"""领域基线：房型房态、门锁凭证、订单、派单、配送、打卡、发票投诉、客人员工、记忆、幂等表

Revision ID: 20260927_0004
Revises: 20260927_0003
Create Date: 2026-09-27

说明：P2 领域迁移。十二张酒店域事实表 + 通用幂等登记表；users 追加账号密码、
状态与最近登录列（管理端账号，P2 认证基座）。凭证表仅存 code_hash 与生效窗口，
以部分唯一索引保证同房间至多一条 active 与一条 next。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927_0004"
down_revision: str | None = "20260927_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))
    op.add_column(
        "users", sa.Column("status", sa.String(length=16), server_default="active", nullable=False)
    )
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "room_types",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("base_price_fen", sa.Integer(), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("bed_type", sa.String(length=32), nullable=False),
        sa.Column("amenities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_room_types_code"),
    )

    op.create_table(
        "rooms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("room_no", sa.String(length=32), nullable=False),
        sa.Column("room_type_id", sa.Uuid(), nullable=False),
        sa.Column("floor", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="available", nullable=False),
        sa.Column("lock_device_no", sa.String(length=64), nullable=True),
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
        sa.ForeignKeyConstraint(["room_type_id"], ["room_types.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_no", name="uq_rooms_room_no"),
    )
    op.create_index("ix_rooms_status", "rooms", ["status"])

    op.create_table(
        "guests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("openid", sa.String(length=64), nullable=True),
        sa.Column("nickname", sa.String(length=64), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_guests_user_id"),
        sa.UniqueConstraint("openid", name="uq_guests_openid"),
    )

    op.create_table(
        "staff",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("staff_no", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("position", sa.String(length=32), server_default="housekeeping", nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("staff_no", name="uq_staff_staff_no"),
        sa.UniqueConstraint("user_id", name="uq_staff_user_id"),
    )
    op.create_index("ix_staff_status", "staff", ["status"])

    op.create_table(
        "lock_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("room_no", sa.String(length=32), nullable=False),
        sa.Column("device_credential_id", sa.String(length=64), nullable=True),
        sa.Column("code_hash", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("reason", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("room_no", "version", name="uq_lock_credentials_room_version"),
        sa.UniqueConstraint("idempotency_key", name="uq_lock_credentials_idempotency"),
    )
    op.create_index(
        "uq_lock_credentials_active_room",
        "lock_credentials",
        ["room_no"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_lock_credentials_next_room",
        "lock_credentials",
        ["room_no"],
        unique=True,
        postgresql_where=sa.text("status = 'next'"),
    )
    op.create_index(
        "ix_lock_credentials_room_created_at", "lock_credentials", ["room_no", "created_at"]
    )

    op.create_table(
        "orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=True),
        sa.Column("room_type_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="created", nullable=False),
        sa.Column("check_in_date", sa.Date(), nullable=False),
        sa.Column("check_out_date", sa.Date(), nullable=False),
        sa.Column("nights", sa.Integer(), nullable=False),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("paid_amount_fen", sa.Integer(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=128), nullable=True),
        sa.Column(
            "events",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.ForeignKeyConstraint(["room_type_id"], ["room_types.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_no", name="uq_orders_order_no"),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),
    )
    op.create_index("ix_orders_guest_created_at", "orders", ["guest_id", "created_at"])
    op.create_index("ix_orders_status_created_at", "orders", ["status", "created_at"])
    op.create_index("ix_orders_room_status", "orders", ["room_id", "status"])

    op.create_table(
        "housekeeping_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task_type", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("room_no", sa.String(length=32), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("assigned_staff_id", sa.Uuid(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["assigned_staff_id"], ["staff.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_housekeeping_tasks_idempotency"),
    )
    op.create_index(
        "ix_housekeeping_tasks_staff_status", "housekeeping_tasks", ["assigned_staff_id", "status"]
    )
    op.create_index(
        "ix_housekeeping_tasks_status_created_at", "housekeeping_tasks", ["status", "created_at"]
    )

    op.create_table(
        "robot_deliveries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("room_no", sa.String(length=32), nullable=False),
        sa.Column("item", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("external_delivery_id", sa.String(length=64), nullable=True),
        sa.Column("fail_reason", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
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
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_robot_deliveries_idempotency"),
    )
    op.create_index(
        "ix_robot_deliveries_guest_created_at", "robot_deliveries", ["guest_id", "created_at"]
    )

    op.create_table(
        "attendance_checkins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("staff_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("photo_url", sa.String(length=255), nullable=False),
        sa.Column("face_score", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("badge_ok", sa.Boolean(), nullable=True),
        sa.Column("uniform_ok", sa.Boolean(), nullable=True),
        sa.Column("review_status", sa.String(length=16), nullable=False),
        sa.Column("reviewed_by", sa.Uuid(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["staff_id"], ["staff.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_attendance_checkins_staff_created_at", "attendance_checkins", ["staff_id", "created_at"]
    )

    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("tax_no", sa.String(length=64), nullable=True),
        sa.Column("amount_fen", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="requested", nullable=False),
        sa.Column("file_url", sa.String(length=255), nullable=True),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_invoices_idempotency"),
    )
    op.create_index("ix_invoices_guest_created_at", "invoices", ["guest_id", "created_at"])

    op.create_table(
        "complaints",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=True),
        sa.Column("room_no", sa.String(length=32), nullable=True),
        sa.Column("category", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_complaints_guest_created_at", "complaints", ["guest_id", "created_at"])

    op.create_table(
        "guest_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("guest_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sensitive", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["guest_id"], ["guests.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_guest_memories_guest_created_at", "guest_memories", ["guest_id", "created_at"]
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("result_type", sa.String(length=64), nullable=True),
        sa.Column("result_ref", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")

    op.drop_index("ix_guest_memories_guest_created_at", table_name="guest_memories")
    op.drop_table("guest_memories")

    op.drop_index("ix_complaints_guest_created_at", table_name="complaints")
    op.drop_table("complaints")

    op.drop_index("ix_invoices_guest_created_at", table_name="invoices")
    op.drop_table("invoices")

    op.drop_index("ix_attendance_checkins_staff_created_at", table_name="attendance_checkins")
    op.drop_table("attendance_checkins")

    op.drop_index("ix_robot_deliveries_guest_created_at", table_name="robot_deliveries")
    op.drop_table("robot_deliveries")

    op.drop_index("ix_housekeeping_tasks_status_created_at", table_name="housekeeping_tasks")
    op.drop_index("ix_housekeeping_tasks_staff_status", table_name="housekeeping_tasks")
    op.drop_table("housekeeping_tasks")

    op.drop_index("ix_orders_room_status", table_name="orders")
    op.drop_index("ix_orders_status_created_at", table_name="orders")
    op.drop_index("ix_orders_guest_created_at", table_name="orders")
    op.drop_table("orders")

    op.drop_index("ix_lock_credentials_room_created_at", table_name="lock_credentials")
    op.drop_index("uq_lock_credentials_next_room", table_name="lock_credentials")
    op.drop_index("uq_lock_credentials_active_room", table_name="lock_credentials")
    op.drop_table("lock_credentials")

    op.drop_index("ix_staff_status", table_name="staff")
    op.drop_table("staff")

    op.drop_table("guests")

    op.drop_index("ix_rooms_status", table_name="rooms")
    op.drop_table("rooms")

    op.drop_table("room_types")

    op.drop_column("users", "last_login_at")
    op.drop_column("users", "status")
    op.drop_column("users", "password_hash")
