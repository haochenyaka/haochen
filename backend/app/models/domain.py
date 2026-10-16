"""酒店领域事实表：房型房态、门锁凭证、订单、派单、配送、打卡、发票投诉、客人员工与记忆。

边界约定：
- 门锁凭证只存 code_hash 与生效窗口，明文只存在于门锁设备与签发时刻的载荷（验收主张 A2）；
- 清洁任务 payload 不得包含明文密码，执行期经门锁服务按需获取；
- 幂等键在具备自然唯一键的对象上以列内唯一约束兜底，跨对象去重走 idempotency_records。
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class RoomType(Base):
    __tablename__ = "room_types"
    __table_args__ = (UniqueConstraint("code", name="uq_room_types_code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    base_price_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    bed_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amenities: Mapped[list[str] | None] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Room(Base):
    """房间档案与房态：available / occupied / cleaning / maintenance。"""

    __tablename__ = "rooms"
    __table_args__ = (
        UniqueConstraint("room_no", name="uq_rooms_room_no"),
        Index("ix_rooms_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    room_no: Mapped[str] = mapped_column(String(32), nullable=False)
    room_type_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("room_types.id"), nullable=False)
    floor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="available")
    lock_device_no: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LockCredential(Base):
    """门锁凭证：同房间同一时刻至多一条 active（另有至多一条 next 预生成）。"""

    __tablename__ = "lock_credentials"
    __table_args__ = (
        UniqueConstraint("room_no", "version", name="uq_lock_credentials_room_version"),
        UniqueConstraint("idempotency_key", name="uq_lock_credentials_idempotency"),
        Index(
            "uq_lock_credentials_active_room",
            "room_no",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_lock_credentials_next_room",
            "room_no",
            unique=True,
            postgresql_where=text("status = 'next'"),
        ),
        Index("ix_lock_credentials_room_created_at", "room_no", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    room_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("rooms.id"), nullable=False)
    room_no: Mapped[str] = mapped_column(String(32), nullable=False)
    device_credential_id: Mapped[str | None] = mapped_column(String(64))
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    reason: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("'1'"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    """订单：状态机见领域术语表；改期/续订以 events 追加事件记录，不作为状态。"""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_orders_order_no"),
        UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),
        Index("ix_orders_guest_created_at", "guest_id", "created_at"),
        Index("ix_orders_status_created_at", "status", "created_at"),
        Index("ix_orders_room_status", "room_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_no: Mapped[str] = mapped_column(String(32), nullable=False)
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    room_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("rooms.id"))
    room_type_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("room_types.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="created")
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    nights: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_amount_fen: Mapped[int | None] = mapped_column(Integer)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(128))
    events: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Payment(Base):
    """支付单：JSAPI 下单尝试与回调事实；transaction_id 唯一，重复回调命中同一事实（A3）。"""

    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("transaction_id", name="uq_payments_transaction_id"),
        Index("ix_payments_order_created_at", "order_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id"), nullable=False)
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    openid: Mapped[str] = mapped_column(String(64), nullable=False)
    prepay_id: Mapped[str | None] = mapped_column(String(64))
    amount_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="created")
    transaction_id: Mapped[str | None] = mapped_column(String(64))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class HousekeepingTask(Base):
    """清洁派单：退房清洁与呼叫清洁；payload 不含明文密码，留证在 evidence。"""

    __tablename__ = "housekeeping_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_housekeeping_tasks_idempotency"),
        Index("ix_housekeeping_tasks_staff_status", "assigned_staff_id", "status"),
        Index("ix_housekeeping_tasks_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    task_type: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    room_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("rooms.id"), nullable=False)
    room_no: Mapped[str] = mapped_column(String(32), nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("orders.id"))
    assigned_staff_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("staff.id"))
    payload: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    evidence: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    guest_score: Mapped[int | None] = mapped_column(Integer)
    guest_comment: Mapped[str | None] = mapped_column(Text)
    rated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class RobotDelivery(Base):
    __tablename__ = "robot_deliveries"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_robot_deliveries_idempotency"),
        Index("ix_robot_deliveries_guest_created_at", "guest_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("orders.id"))
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    room_no: Mapped[str] = mapped_column(String(32), nullable=False)
    item: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    external_delivery_id: Mapped[str | None] = mapped_column(String(64))
    fail_reason: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AttendanceCheckin(Base):
    """打卡审核：未通过进入 manual_review，不允许静默通过。"""

    __tablename__ = "attendance_checkins"
    __table_args__ = (Index("ix_attendance_checkins_staff_created_at", "staff_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    staff_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("staff.id"), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    photo_url: Mapped[str] = mapped_column(String(255), nullable=False)
    face_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    badge_ok: Mapped[bool | None] = mapped_column(Boolean)
    uniform_ok: Mapped[bool | None] = mapped_column(Boolean)
    review_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewed_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_invoices_idempotency"),
        Index("ix_invoices_guest_created_at", "guest_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id"), nullable=False)
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    tax_no: Mapped[str | None] = mapped_column(String(64))
    amount_fen: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="requested")
    file_url: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Complaint(Base):
    __tablename__ = "complaints"
    __table_args__ = (Index("ix_complaints_guest_created_at", "guest_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("orders.id"))
    room_no: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Guest(Base):
    """客人档案：user_id 为接入身份链接（P4 微信登录后经 openid 建立）。"""

    __tablename__ = "guests"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_guests_user_id"),
        UniqueConstraint("openid", name="uq_guests_openid"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    openid: Mapped[str | None] = mapped_column(String(64))
    nickname: Mapped[str | None] = mapped_column(String(64))
    phone: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Staff(Base):
    """员工档案：staff_no 为员工端登录工号；position 目前为 housekeeping。"""

    __tablename__ = "staff"
    __table_args__ = (
        UniqueConstraint("staff_no", name="uq_staff_staff_no"),
        UniqueConstraint("user_id", name="uq_staff_user_id"),
        Index("ix_staff_status", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    staff_no: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[str] = mapped_column(String(32), nullable=False, server_default="housekeeping")
    user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class GuestMemory(Base):
    __tablename__ = "guest_memories"
    __table_args__ = (Index("ix_guest_memories_guest_created_at", "guest_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    guest_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("guests.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    source: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecord(Base):
    """通用幂等组件：(scope, key) 唯一，重复提交返回既有结果引用而非新建副作用。"""

    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("scope", "key", name="uq_idempotency_scope_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    result_type: Mapped[str | None] = mapped_column(String(64))
    result_ref: Mapped[UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HotelConfig(Base):
    """酒店配置：单行记录，id 恒为 1；通过 GET/PUT 读写。"""

    __tablename__ = "hotel_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    hotel_name: Mapped[str] = mapped_column(String(128), nullable=False, server_default="皓晨酒店")
    address: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(32))
    brand: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
