"""清空知识库语料，供重新入库（dev 工具，不进运行时）。

`seed_knowledge` 按 `(kb_id, checksum)` 幂等：正文或分块规则变化后，已入库文档不会重新
切分，分块级黄金锚点的 `ordinal` 会与检索结果错位。重建顺序：

1. Milvus：删除本库前缀下的向量集合（纯派生数据，下次入库重建）；
2. MinIO：删除原件与解析产物；
3. PostgreSQL：先删 `kb_chunks`（外键指向 `kb_documents`），再删 `kb_documents`。

知识库行本身保留（`seed_knowledge` 按 code 复用）。清理后重跑：
    python -m app.knowledge.seed

命令：
    python reset_knowledge.py          # 只统计将删除的数量
    python reset_knowledge.py --yes    # 实际执行
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence

from pymilvus import MilvusClient
from sqlalchemy import delete, func, select

from app.config import get_settings
from app.db import SessionFactory
from app.knowledge.seed import KB_CODE
from app.knowledge.storage import ObjectStorage
from app.models.knowledge import KbChunk, KbDocument, KnowledgeBase


def _kb_collections() -> list[str]:
    """按配置前缀列集合：不依赖 embedding identity，避免 provider 变更后漏删。"""
    settings = get_settings()
    client = MilvusClient(uri=settings.milvus_uri)
    prefix = settings.milvus_collection_prefix
    return sorted(name for name in client.list_collections() if name.startswith(prefix))


def _drop_collections(names: Sequence[str]) -> None:
    if not names:
        return
    client = MilvusClient(uri=get_settings().milvus_uri)
    for name in names:
        client.drop_collection(name)


async def reset(*, apply: bool) -> None:
    async with SessionFactory() as session:
        kb = (
            await session.execute(select(KnowledgeBase).where(KnowledgeBase.code == KB_CODE))
        ).scalar_one_or_none()
        if kb is None:
            print(f"知识库 {KB_CODE} 不存在，无需清理")
            return

        documents = (
            await session.execute(
                select(KbDocument.object_key, KbDocument.parse_object_key, KbDocument.status).where(
                    KbDocument.kb_id == kb.id
                )
            )
        ).all()
        chunks = (
            await session.execute(
                select(func.count()).select_from(KbChunk).where(KbChunk.kb_id == kb.id)
            )
        ).scalar_one()
        collections = _kb_collections()
        statuses = sorted({status for _, _, status in documents})

        print(f"kb={kb.code} id={kb.id}")
        print(f"documents={len(documents)} chunks={chunks} collections={collections}")
        print(f"document statuses={statuses}")
        if not apply:
            print("dry-run：加 --yes 实际清理")
            return

        _drop_collections(collections)

        storage = ObjectStorage.from_settings()
        for object_key, parse_key, _ in documents:
            await storage.remove(object_key)
            if parse_key:
                await storage.remove(parse_key)

        await session.execute(delete(KbChunk).where(KbChunk.kb_id == kb.id))
        await session.execute(delete(KbDocument).where(KbDocument.kb_id == kb.id))
        await session.commit()

    print("清理完成；下一步：python -m app.knowledge.seed")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清空知识库语料以便重新入库")
    parser.add_argument("--yes", action="store_true", help="实际执行清理（默认只统计）")
    args = parser.parse_args(argv)
    asyncio.run(reset(apply=args.yes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
