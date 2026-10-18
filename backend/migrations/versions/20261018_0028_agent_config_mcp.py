"""agent_config 增加 enabled_mcp_servers 列（PR-M2 运行期接线）

MCP 服务器此前只在管理端配置、运行时零接线。PR-M2 让管理端启用的 MCP server 真正
注入 Agent 运行期：`enabled_mcp_servers` 承载「按 Agent 启用的 MCP server slug 列表」。

Revision ID: 20261018_0028
Revises: 20261018_0027
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261018_0028"
down_revision: str | None = "20261018_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_config",
        sa.Column("enabled_mcp_servers", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_config", "enabled_mcp_servers")