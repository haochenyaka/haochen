"""创建 agent_config 表

Agent 配置持久化：config_json 为管理端配置快照，enabled_tools/skills/knowledge_bases
为管理员显式勾选的资源列表（null 表示使用代码声明默认值）。

Revision ID: 20260930_0013
Revises: 20260929_0012
Create Date: 2026-09-30

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "20260930_0013"
down_revision: str | None = "20260929_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_config",
        sa.Column("agent_slug", sa.String(64), nullable=False),
        sa.Column("config_json", JSONB(), nullable=True),
        sa.Column("enabled_tools", JSONB(), nullable=True),
        sa.Column("enabled_skills", JSONB(), nullable=True),
        sa.Column("enabled_knowledge_bases", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("agent_slug"),
    )


def downgrade() -> None:
    op.drop_table("agent_config")
