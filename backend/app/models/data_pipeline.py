"""数据中台模型：清洗规则、管道运行记录、归档数据。

不变量：
- 规则与运行为一对多：一条规则可触发多次运行；
- 管道运行是 append-only：每次触发产生新行，历史运行保留作为审计。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Integer, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DataCleaningRule(Base):
    """数据清洗规则配置：定义清洗任务类型、调度周期、启停与参数。"""

    __tablename__ = "data_cleaning_rules"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    """规则名称，如「会话数据脱敏」"""
    description: Mapped[str | None] = mapped_column(Text)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    """对应 durable task 的 task_type，如 data_pipeline.clean_conversations"""
    handler_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    schedule_cron: Mapped[str | None] = mapped_column(String(64))
    """cron 表达式；为空表示仅手动触发"""
    config: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """规则参数，如保留天数、脱敏字段列表、批处理大小等"""
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class DataPipelineRun(Base):
    """数据管道运行记录：一次清洗任务的执行快照。"""

    __tablename__ = "data_pipeline_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    rule_id: Mapped[int | None] = mapped_column(BigInteger)
    """关联规则 ID；null 表示一次性任务"""
    task_id: Mapped[UUID | None] = mapped_column(Uuid)
    """关联的 durable task ID；null 表示尚未创建 task"""
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    """触发方式：manual / scheduled / api"""
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )
    """pending / running / completed / failed"""
    source_table: Mapped[str | None] = mapped_column(String(64))
    """清洗的数据源表名"""
    processed_rows: Mapped[int | None] = mapped_column(Integer)
    affected_rows: Mapped[int | None] = mapped_column(Integer)
    error_rows: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    """执行摘要，如{"脱敏字段": ["phone", "id_card"], "脱敏行数": 120}"""
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DataArchive(Base):
    """数据归档表：从主表迁出的历史记录。"""

    __tablename__ = "data_archives"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    """来源表名，如 model_audit"""
    source_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """来源表的主键 ID"""
    pipeline_run_id: Mapped[UUID | None] = mapped_column(Uuid)
    """触发归档的管道运行 ID"""
    data: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    """归档时的完整数据快照"""
    archived_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )