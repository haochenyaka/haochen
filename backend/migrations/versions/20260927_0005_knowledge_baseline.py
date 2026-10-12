"""知识库基线：知识库、文档状态机与分块正文

Revision ID: 20260927_0005
Revises: 20260927_0004
Create Date: 2026-09-27

说明：P3 知识库迁移。三张表——knowledge_bases（配置与可见范围）、kb_documents
（原件元数据 + 状态机 + claim/lease）、kb_chunks（分块正文，检索回填与引用溯源）。
checksum 与 kb_id 组合唯一：同库同内容不重复上传；parsing/indexing 的在途状态用
claim_owner + claim_expires_at 表达，条件抢占更新。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0005"
down_revision: str | None = "20260927_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(length=16), server_default="guest", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_knowledge_bases_code"),
    )

    op.create_table(
        "kb_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("parse_object_key", sa.String(length=512), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="uploaded", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("claim_owner", sa.String(length=64), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kb_id", "checksum", name="uq_kb_documents_content"),
    )
    op.create_index("ix_kb_documents_kb_status", "kb_documents", ["kb_id", "status"])
    op.create_index("ix_kb_documents_status_created_at", "kb_documents", ["status", "created_at"])
    op.create_index(
        "ix_kb_documents_claim_expires",
        "kb_documents",
        ["claim_expires_at"],
        postgresql_where=sa.text("status IN ('parsing', 'indexing')"),
    )

    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=True),
        sa.Column("char_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_bases.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["kb_documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_kb_chunks_ordinal"),
    )
    op.create_index("ix_kb_chunks_kb_id_ordinal", "kb_chunks", ["kb_id", "document_id", "ordinal"])


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_kb_id_ordinal", table_name="kb_chunks")
    op.drop_table("kb_chunks")

    op.drop_index("ix_kb_documents_claim_expires", table_name="kb_documents")
    op.drop_index("ix_kb_documents_status_created_at", table_name="kb_documents")
    op.drop_index("ix_kb_documents_kb_status", table_name="kb_documents")
    op.drop_table("kb_documents")

    op.drop_table("knowledge_bases")
