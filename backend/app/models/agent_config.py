from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgentConfig(Base):
    """Agent 持久化配置：按 agent_slug 唯一存储管理端写入的配置快照。

    运行时若 DB 无该记录表示使用代码声明的全部默认值；config_json 仅保存
    用户在管理端显式修改的字段，不冗余保存默认值。
    """

    __tablename__ = "agent_config"

    agent_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """用户在管理端配置的键值对，传入 BaseContext.from_config()。"""

    enabled_tools: Mapped[list[str] | None] = mapped_column(JSONB)
    """管理员显式勾选启用的工具 name 列表；null 表示使用 agent 代码声明的 tool_ids。"""

    enabled_skills: Mapped[list[str] | None] = mapped_column(JSONB)
    """管理员显式勾选启用的 skill slug 列表；null 表示使用 agent 代码声明的 skill_ids。"""

    enabled_knowledge_bases: Mapped[list[str] | None] = mapped_column(JSONB)
    """管理员显式勾选启用的知识库代码列表；null 表示使用 agent 代码声明的 knowledge_base_ids。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
