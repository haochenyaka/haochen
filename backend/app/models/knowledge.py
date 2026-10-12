"""知识库事实表：知识库配置、文档状态机与分块正文。

存储分工（机制基线 §2.8）：PostgreSQL 拥有配置、权限、文件元数据与状态、chunk 正文
与 claim/lease；MinIO 拥有原件与解析产物；Milvus 拥有向量与 BM25 字段。Milvus 侧写入
是补偿式双写——失败置 error_indexing 并可重试，PG 行始终是权威状态。

不变量：文档正文只存 PG（kb_chunks）；原件与解析产物以 object_key 引用，不落库；
checksum 天然唯一（同库同内容不重复上传）；parsing/indexing 为条件抢占，并发不双写。
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeBase(Base):
    """知识库：可见范围决定哪些 Agent 可在检索工具中引用（执行处复验）。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("code", name="uq_knowledge_bases_code"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # guest：C 端可检索；staff：员工端可检索；internal：仅内部（客服/管理）
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default="guest")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KbDocument(Base):
    """知识库文档：状态机 uploaded → parsing → parsed → indexing → indexed。"""

    __tablename__ = "kb_documents"
    __table_args__ = (
        UniqueConstraint("kb_id", "checksum", name="uq_kb_documents_content"),
        Index("ix_kb_documents_kb_status", "kb_id", "status"),
        Index("ix_kb_documents_status_created_at", "status", "created_at"),
        Index(
            "ix_kb_documents_claim_expires",
            "claim_expires_at",
            postgresql_where=text("status IN ('parsing', 'indexing')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kb_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("knowledge_bases.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    parse_object_key: Mapped[str | None] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="uploaded")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    claim_owner: Mapped[str | None] = mapped_column(String(64))
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KbChunk(Base):
    """分块正文：检索命中后回填上下文与引用，Milvus 只存向量与 BM25 字段。"""

    __tablename__ = "kb_chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "ordinal", name="uq_kb_chunks_ordinal"),
        Index("ix_kb_chunks_kb_id_ordinal", "kb_id", "document_id", "ordinal"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kb_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("knowledge_bases.id"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("kb_documents.id"), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
