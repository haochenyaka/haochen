from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class McpServer(Base):
    """MCP 服务器配置：管理端可创建/编辑/启用/停用 MCP 服务端点。

    api_key/secret 等凭证不落库（验收主张 A2），由外部密钥管理或运行时注入。
    传输仅接受远程形态 `streamable_http` / `sse`（`stdio` 仅限代码维护的系统内置项）。
    """

    __tablename__ = "mcp_servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    server_url: Mapped[str] = mapped_column(String(512), nullable=False)
    transport: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="streamable_http"
    )
    """远程传输形态：streamable_http（默认）| sse。stdio 不允许。"""
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """额外配置：timeout_seconds、tool_prefix、read_only_tools、dangerous_tools、
    disabled_tools 等（不含凭证）。"""
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    # 最近一次连通性探测结论（由管理端「测试连接」写入）
    last_status: Mapped[str | None] = mapped_column(String(32))
    """ok | failed | unknown（从未探测）"""
    last_error: Mapped[str | None] = mapped_column(Text)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_tools: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    """最近一次发现到的工具清单：[{name, description}, ...]"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
