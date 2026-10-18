"""MCP 服务器增加 transport 与探测结论列（PR-M1）

背景：MCP 此前只有配置表 + CRUD，运行时零接线。PR-M1 引入 MCP 客户端与连通性测试，
需要承载传输形态与最近一次探测结论：
  - transport：远程传输白名单 streamable_http / sse（stdio 仅限系统内置项）
  - last_status / last_error / last_tested_at / discovered_tools：探测结论落库，
    供管理端列表展示，避免每次打外部网络。

Revision ID: 20261018_0027
Revises: 20261018_0026
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261018_0027"
down_revision: str | None = "20261018_0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mcp_servers",
        sa.Column("transport", sa.String(32), nullable=False, server_default="streamable_http"),
    )
    op.add_column("mcp_servers", sa.Column("last_status", sa.String(32), nullable=True))
    op.add_column("mcp_servers", sa.Column("last_error", sa.Text(), nullable=True))
    op.add_column(
        "mcp_servers", sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "mcp_servers", sa.Column("discovered_tools", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("mcp_servers", "discovered_tools")
    op.drop_column("mcp_servers", "last_tested_at")
    op.drop_column("mcp_servers", "last_error")
    op.drop_column("mcp_servers", "last_status")
    op.drop_column("mcp_servers", "transport")