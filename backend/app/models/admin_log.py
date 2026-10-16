from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AdminActionLog(Base):
    """管理操作留痕：软删审计行等后台操作的不可变审计痕迹（验收主张 A2/A4 的写操作可追溯）。

    每条记录一个操作批次，target_ids 列出被操作的审计行 id（JSONB int 数组），
    detail 存放额外上下文（如请求体摘要）。created_at 设为主键的最后字段。
    """

    __tablename__ = "admin_action_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id: Mapped[UUID] = mapped_column(PgUuid, nullable=False)
    actor_username: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_ids: Mapped[list[object]] = mapped_column(JSONB, nullable=False)
    target_count: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
