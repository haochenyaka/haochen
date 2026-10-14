from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpServer(Base):
    """MCP 服务器配置：管理端可创建/编辑/启用/停用 MCP 服务端点。

    api_key/secret 等凭证不落库（验收主张 A2），由外部密钥管理或运行时注入。
    """

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    server_url: Mapped[str] = mapped_column(String(512), nullable=False)
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """额外配置：超时、自定义请求头（不含凭证）、模型映射等。"""
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
