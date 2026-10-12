"""agent_runs 增加 first_token_at（首 token 延迟 TTFT 埋点）

P7.4/P7.9 报表需要首 token 延迟 P50/P95。首个 message.delta 事件发出时把当前时间
写入该列（仅当为 NULL 时写，幂等），TTFT = first_token_at - started_at。可空，
无增量即失败/取消的 Run 保持 NULL、不计入分位。

Revision ID: 20260929_0010
Revises: 20260929_0009
Create Date: 2026-09-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0010"
down_revision: str | None = "20260929_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs",
        sa.Column("first_token_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "first_token_at")
