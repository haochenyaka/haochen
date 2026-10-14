"""一次性脚本：导出审计表到 MinIO Parquet 并构建预聚合。

等价于依次触发：
  data_pipeline.export_messages
  data_pipeline.export_audits
  data_pipeline.build_aggregates

但直接执行逻辑，不依赖 ARQ worker。
"""

import asyncio
import logging
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_pipeline.lake_storage import LakeStorage
from app.db import SessionFactory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_build_aggregates")


async def _export_table(
    session: AsyncSession,
    storage: LakeStorage,
    *,
    table: str,
    columns: list[str],
    batch_size: int,
    checkpoint_col: str,
    checkpoint: int | None = None,
    where_extra: str = "",
) -> dict[str, Any]:
    """直接从 PG 查询并写入 MinIO Parquet（绕过 ARQ）。"""
    total_rows = 0
    offset = 0
    max_id = checkpoint or 0
    today = date.today()
    col_list = ", ".join(columns)
    current_checkpoint = checkpoint if checkpoint is not None else 0

    while True:
        where_clauses = [f"{checkpoint_col} > :checkpoint"]
        if where_extra:
            where_clauses.append(where_extra)
        where_sql = " AND ".join(where_clauses)

        params: dict[str, Any] = {"limit": batch_size, "checkpoint": current_checkpoint}

        rows = (
            (
                await session.execute(
                    text(f"""
                    SELECT {col_list}
                    FROM {table}
                    WHERE {where_sql}
                    ORDER BY {checkpoint_col}
                    LIMIT :limit
                """),
                    params,
                )
            )
            .mappings()
            .all()
        )

        if not rows:
            break

        batch = []
        for r in rows:
            row_dict = dict(r)
            for k, v in row_dict.items():
                if isinstance(v, datetime):
                    row_dict[k] = v.isoformat()
                elif isinstance(v, date):
                    row_dict[k] = v.isoformat()
            batch.append(row_dict)
            current_id = row_dict.get(checkpoint_col, 0)
            if current_id and current_id > max_id:
                max_id = current_id

        if max_id > current_checkpoint:
            current_checkpoint = max_id

        part_offset = offset // batch_size
        await storage.write_parquet(table, today, part_offset, batch)
        total_rows += len(batch)
        offset += len(batch)
        logger.info("  %s: exported %d rows (max_id=%s)", table, total_rows, max_id)

    return {"exported": total_rows, "max_id": max_id}


async def export_messages_direct(storage: LakeStorage) -> dict[str, Any]:
    """导出 messages 表。"""
    logger.info("=== Exporting messages ===")
    async with SessionFactory() as session:
        result = await _export_table(
            session,
            storage,
            table="messages",
            columns=[
                "seq",
                "thread_id",
                "role",
                "content::text as content_text",
                "metadata::text as metadata_text",
                "run_id::text as run_id_text",
                "created_at",
            ],
            batch_size=2000,
            checkpoint_col="seq",
        )
    logger.info("  messages exported: %d rows", result["exported"])
    return result


async def export_audits_direct(storage: LakeStorage) -> dict[str, Any]:
    """导出 model_audit 和 tool_audit 表。"""
    total = 0
    for table_name in ["model_audit", "tool_audit"]:
        logger.info("=== Exporting %s ===", table_name)
        cols = [
            "id",
            "run_id::text as run_id_text",
            "agent_slug",
            "status",
            "duration_ms",
            "error",
        ]
        if table_name == "model_audit":
            cols += [
                "model",
                "prompt_tokens",
                "completion_tokens",
                "cached_prompt_tokens",
                "created_at",
            ]
        else:
            cols += ["tool_name", "created_at"]

        async with SessionFactory() as session:
            result = await _export_table(
                session,
                storage,
                table=table_name,
                columns=cols,
                batch_size=2000,
                checkpoint_col="id",
            )
        total += result["exported"]
        logger.info("  %s exported: %d rows", table_name, result["exported"])
    return {"exported": total}


async def build_aggregates_direct(storage: LakeStorage) -> dict[str, Any]:
    """使用 DuckDB 读取 Parquet 文件，生成预聚合表。"""

    import duckdb

    logger.info("=== Building aggregates ===")

    audit_files = await storage.list_partitions("model_audit")
    tool_files = await storage.list_partitions("tool_audit")

    if not audit_files and not tool_files:
        logger.warning("No Parquet files found in data lake")
        return {"status": "skipped", "reason": "no parquet files found"}

    con = duckdb.connect()
    try:
        results: dict[str, int] = {}

        # Token 聚合
        if audit_files:
            local_paths = [str(await storage.read_parquet_to_temp(f)) for f in audit_files]
            con.execute(f"""
                CREATE VIEW model_audit_v AS
                SELECT * FROM read_parquet({local_paths})
            """)

            agg = con.execute("""
                SELECT
                    CAST(created_at AS DATE) AS day,
                    model,
                    COUNT(*) AS calls,
                    COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                    COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                    COALESCE(SUM(cached_prompt_tokens), 0) AS cached_tokens
                FROM model_audit_v
                WHERE created_at IS NOT NULL
                GROUP BY day, model
                ORDER BY day, model
            """).fetch_arrow_table()

            if agg.num_rows > 0:
                await storage.write_parquet("agg_token_daily", date.today(), 0, agg.to_pylist())
                results["token_agg"] = agg.num_rows
                logger.info("  agg_token_daily: %d rows", agg.num_rows)

        # 工具调用聚合
        if tool_files:
            local_paths = [str(await storage.read_parquet_to_temp(f)) for f in tool_files]
            con.execute(f"""
                CREATE VIEW tool_audit_v AS
                SELECT * FROM read_parquet({local_paths})
            """)

            tool_agg = con.execute("""
                SELECT
                    CAST(created_at AS DATE) AS day,
                    tool_name,
                    status,
                    COUNT(*) AS calls,
                    AVG(duration_ms) AS avg_duration_ms
                FROM tool_audit_v
                WHERE created_at IS NOT NULL
                GROUP BY day, tool_name, status
                ORDER BY day, tool_name
            """).fetch_arrow_table()

            if tool_agg.num_rows > 0:
                await storage.write_parquet("agg_tool_daily", date.today(), 0, tool_agg.to_pylist())
                results["tool_agg"] = tool_agg.num_rows
                logger.info("  agg_tool_daily: %d rows", tool_agg.num_rows)

        return {"status": "completed", "results": results}
    finally:
        con.close()


async def main() -> None:
    storage = LakeStorage.from_settings()
    await storage.ensure_bucket()
    logger.info("Data lake bucket ready")

    step1 = await export_messages_direct(storage)
    step2 = await export_audits_direct(storage)
    step3 = await build_aggregates_direct(storage)

    logger.info("=== Summary ===")
    logger.info("export_messages: %s", step1)
    logger.info("export_audits: %s", step2)
    logger.info("build_aggregates: %s", step3)


if __name__ == "__main__":
    asyncio.run(main())
