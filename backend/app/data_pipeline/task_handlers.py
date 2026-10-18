"""数据中台清洗管道：durable task 处理器。

每个清洗任务是一个独立的 @task_handler，注册到 task_registry。
遵循已有模式：使用 ctx.session_factory() 获取会话、DB 操作后提交、长任务报告进度。

安全不变量：
- 脱敏操作不修改原表业务语义字段，只对 content 文本做正则替换；
- 已软删除的记录不参与导出和归档；
- 所有 SQL 使用参数化查询，不拼接字符串。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from uuid import UUID

from app.services.task_registry import TaskContext, task_handler

logger = logging.getLogger(__name__)

# 手机号正则：1xx-xxxx-xxxx
_PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
# 身份证号正则：18 位数字 + X/x
_ID_CARD_PATTERN = re.compile(
    r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b"
)


CHECKPOINT_SCOPE_KEY = "_scope"


async def _complete_run(
    ctx: TaskContext,
    *,
    checkpoint_scope: str | None = None,
    checkpoint: Any = None,
    **kwargs: Any,
) -> None:
    """调用 DataPipelineService.complete_run 更新运行记录。

    `checkpoint_scope` / `checkpoint` 由本函数并入 `summary`：checkpoint 的读取键与写入
    键由此统一（此前导出管道用 task_type 查、用 source_table 写，键永不匹配，增量失效）。
    """
    run_id_str = ctx.payload_value("pipeline_run_id")
    if not run_id_str:
        return
    from app.services.data_pipeline_service import DataPipelineService

    summary = dict(kwargs.get("summary") or {})
    if checkpoint_scope is not None:
        summary[CHECKPOINT_SCOPE_KEY] = checkpoint_scope
    if checkpoint is not None:
        summary["checkpoint"] = checkpoint
    if summary:
        kwargs["summary"] = summary

    async with ctx.session_factory() as session:
        svc = DataPipelineService(session)
        await svc.complete_run(UUID(run_id_str), **kwargs)
        await session.commit()


# ---------------------------------------------------------------------------
# 管道 1：会话数据脱敏
# ---------------------------------------------------------------------------


@task_handler("data_pipeline.clean_conversations", 1)
async def clean_conversations(ctx: TaskContext) -> Mapping[str, Any]:
    """扫描 messages.content，对手机号和身份证号做脱敏处理。

    配置（config）：
      - batch_size: 每批处理行数，默认 500
      - dry_run: 仅统计不写入，默认 false
    """
    config = dict(ctx.payload.get("config", {}) or {})
    batch_size = config.get("batch_size", 500)
    dry_run = config.get("dry_run", False)

    start = time.time()
    total_processed = 0
    total_affected = 0
    total_errors = 0
    offset = 0

    try:
        while True:
            async with ctx.session_factory() as session:
                from sqlalchemy import text

                rows = (
                    (
                        await session.execute(
                            text("""
                        SELECT seq, content::text
                        FROM messages
                        WHERE role IN ('assistant', 'tool')
                        ORDER BY seq
                        LIMIT :limit OFFSET :offset
                    """),
                            {"limit": batch_size, "offset": offset},
                        )
                    )
                    .mappings()
                    .all()
                )

                if not rows:
                    break

                batch_affected = 0
                for row in rows:
                    content: str = row["content"]
                    if not content:
                        continue
                    masked = _PHONE_PATTERN.sub("***", content)
                    masked = _ID_CARD_PATTERN.sub("***", masked)
                    if masked != content:
                        batch_affected += 1
                        if not dry_run:
                            await session.execute(
                                text("""
                                    UPDATE messages
                                    SET content = jsonb_set(
                                        content, '{text}', to_jsonb(:masked::text)
                                    )
                                    WHERE seq = :seq
                                """),
                                {"seq": row["seq"], "masked": masked},
                            )

                total_processed += len(rows)
                total_affected += batch_affected
                offset += batch_size

                if not dry_run:
                    await session.commit()

                # 报告进度
                if not dry_run:
                    async with ctx.session_factory() as progress_session:
                        from app.repositories.task_repository import TaskRepository

                        repo = TaskRepository(progress_session)
                        await repo.set_progress(
                            ctx.task_id,
                            owner_token=ctx.owner_token,
                            progress={"processed": total_processed, "affected": total_affected},
                        )
                        await progress_session.commit()

        duration = time.time() - start
        await _complete_run(
            ctx,
            status="completed",
            source_table="messages",
            processed_rows=total_processed,
            affected_rows=total_affected,
            error_rows=total_errors,
            duration_seconds=duration,
            summary={"fields_masked": ["phone", "id_card"], "dry_run": dry_run},
        )
    except Exception as e:
        duration = time.time() - start
        logger.exception("clean_conversations 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table="messages",
            processed_rows=total_processed,
            affected_rows=total_affected,
            error_rows=total_errors,
            duration_seconds=duration,
            error=str(e),
        )
        raise

    return {"processed": total_processed, "affected": total_affected, "errors": total_errors}


# ---------------------------------------------------------------------------
# 管道 2：反馈数据去重
# ---------------------------------------------------------------------------


@task_handler("data_pipeline.dedup_feedback", 1)
async def dedup_feedback(ctx: TaskContext) -> Mapping[str, Any]:
    """按 (message_id, user_id) 合并多条反馈记录，保留最新一条。"""
    config = dict(ctx.payload.get("config", {}) or {})
    dry_run = config.get("dry_run", False)

    start = time.time()
    total_dupes = 0
    removed = 0
    errors = 0

    try:
        async with ctx.session_factory() as session:
            from sqlalchemy import text

            dupes = (
                (
                    await session.execute(
                        text("""
                    SELECT message_id, user_id, COUNT(*) as cnt
                    FROM message_feedback
                    GROUP BY message_id, user_id
                    HAVING COUNT(*) > 1
                """),
                    )
                )
                .mappings()
                .all()
            )

            total_dupes = len(dupes)
            for dup in dupes:
                try:
                    rows_to_delete: Sequence[Any] = (
                        (
                            await session.execute(
                                text("""
                            SELECT id FROM message_feedback
                            WHERE message_id = :mid AND user_id = :uid
                            ORDER BY created_at DESC
                            OFFSET 1
                        """),
                                {"mid": dup["message_id"], "uid": dup["user_id"]},
                            )
                        )
                        .scalars()
                        .all()
                    )

                    if rows_to_delete and not dry_run:
                        for row_id in rows_to_delete:
                            await session.execute(
                                text("DELETE FROM message_feedback WHERE id = :id"),
                                {"id": row_id},
                            )
                    removed += len(rows_to_delete)
                except Exception:
                    errors += 1

            if not dry_run:
                await session.commit()

        duration = time.time() - start
        await _complete_run(
            ctx,
            status="completed",
            source_table="message_feedback",
            processed_rows=total_dupes,
            affected_rows=removed,
            error_rows=errors,
            duration_seconds=duration,
            summary={"duplicate_groups": total_dupes, "dry_run": dry_run},
        )
    except Exception as e:
        duration = time.time() - start
        logger.exception("dedup_feedback 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table="message_feedback",
            processed_rows=total_dupes,
            affected_rows=removed,
            error_rows=errors,
            duration_seconds=duration,
            error=str(e),
        )
        raise

    return {"duplicate_groups": total_dupes, "removed": removed, "errors": errors}


# ---------------------------------------------------------------------------
# 管道 3：审计记录归档
# ---------------------------------------------------------------------------


@task_handler("data_pipeline.archive_audit_logs", 1)
async def archive_audit_logs(ctx: TaskContext) -> Mapping[str, Any]:
    """将 N 天前的审计记录从主表归档到 data_archives，主表软删除。

    配置（config）：
      - retention_days: 保留天数，默认 90
      - tables: 要归档的表名列表，默认 [model_audit, tool_audit]
      - batch_size: 每批归档行数，默认 1000
      - dry_run: 仅统计不写入，默认 false

    失败语义：任一表抛错即整体 `failed`（写 error），不再"逐表吞异常仍报 completed"。
    """
    config = dict(ctx.payload.get("config", {}) or {})
    retention_days = config.get("retention_days", 90)
    tables = config.get("tables", ["model_audit", "tool_audit"])
    batch_size = int(config.get("batch_size", 1000))
    dry_run = config.get("dry_run", False)

    start = time.time()
    total_archived = 0
    failed_tables: list[str] = []

    try:
        for table_name in tables:
            archived_for_table = 0
            try:
                while True:
                    async with ctx.session_factory() as session:
                        from sqlalchemy import text

                        rows = (
                            (
                                await session.execute(
                                    text(f"""
                                SELECT * FROM {table_name}
                                WHERE created_at < now() - :retention * INTERVAL '1 day'
                                  AND deleted_at IS NULL
                                ORDER BY created_at
                                LIMIT :limit
                            """),
                                    {"retention": retention_days, "limit": batch_size},
                                )
                            )
                            .mappings()
                            .all()
                        )

                        if not rows:
                            break

                        for row in rows:
                            source_id = row.get("id")
                            if source_id is None:
                                continue
                            if not dry_run:
                                await session.execute(
                                    text("""
                                        INSERT INTO data_archives
                                            (source_table, source_id, data)
                                        VALUES (:tbl, :sid, CAST(:data AS jsonb))
                                    """),
                                    {
                                        "tbl": table_name,
                                        "sid": source_id,
                                        "data": json.dumps(dict(row), default=str),
                                    },
                                )
                                soft_delete_sql = (
                                    f"UPDATE {table_name} SET deleted_at = now() WHERE id = :sid"
                                )
                                await session.execute(text(soft_delete_sql), {"sid": source_id})
                            archived_for_table += 1

                        if not dry_run:
                            await session.commit()

                    # dry_run 下不写库，靠下方推进分页会死循环；此时只取一批即停
                    if dry_run:
                        break

                total_archived += archived_for_table
            except Exception:
                logger.exception("归档 %s 失败", table_name)
                failed_tables.append(table_name)

        duration = time.time() - start
        summary = {
            "retention_days": retention_days,
            "tables": tables,
            "batch_size": batch_size,
            "dry_run": dry_run,
            "failed_tables": failed_tables,
        }
        if failed_tables:
            await _complete_run(
                ctx,
                status="failed",
                source_table=",".join(tables),
                processed_rows=total_archived,
                affected_rows=total_archived,
                error_rows=len(failed_tables),
                duration_seconds=duration,
                summary=summary,
                error=f"归档失败的表：{','.join(failed_tables)}",
            )
        else:
            await _complete_run(
                ctx,
                status="completed",
                source_table=",".join(tables),
                processed_rows=total_archived,
                affected_rows=total_archived,
                error_rows=0,
                duration_seconds=duration,
                summary=summary,
            )
    except Exception as e:
        duration = time.time() - start
        logger.exception("archive_audit_logs 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table=",".join(tables),
            processed_rows=total_archived,
            affected_rows=total_archived,
            error_rows=len(failed_tables),
            duration_seconds=duration,
            error=str(e),
        )
        raise

    return {"archived": total_archived, "failed_tables": failed_tables}


# ---------------------------------------------------------------------------
# 管道 4：知识库一致性校验
# ---------------------------------------------------------------------------


@task_handler("data_pipeline.validate_kb_consistency", 1)
async def validate_kb_consistency(ctx: TaskContext) -> Mapping[str, Any]:
    """检测知识库中的冲突和过期文档。

    检测项：
      - 超过 freshness_days 天未更新的过期文档；
      - content 为空或过短的无效块；
      - 同一知识库内 content_hash 重复的疑似重复块。
    """
    config = dict(ctx.payload.get("config", {}) or {})
    freshness_days = config.get("freshness_days", 180)
    min_chunk_length = config.get("min_chunk_length", 10)

    start = time.time()

    try:
        async with ctx.session_factory() as session:
            from sqlalchemy import text

            stale_docs = (
                await session.execute(
                    text("""
                    SELECT COUNT(*) as cnt FROM kb_documents
                    WHERE updated_at < now() - :days * INTERVAL '1 day'
                      AND status = 'indexed'
                """),
                    {"days": freshness_days},
                )
            ).scalar() or 0

            empty_chunks = (
                await session.execute(
                    text("""
                    SELECT COUNT(*) as cnt FROM kb_chunks
                    WHERE length(content) < :min_len
                """),
                    {"min_len": min_chunk_length},
                )
            ).scalar() or 0

            dup_groups = (
                await session.execute(
                    text("""
                    SELECT COUNT(*) as cnt FROM (
                        SELECT content_hash, kb_id
                        FROM kb_chunks
                        WHERE content_hash IS NOT NULL
                        GROUP BY content_hash, kb_id
                        HAVING COUNT(*) > 1
                    ) dup
                """),
                )
            ).scalar() or 0

        total_issues = stale_docs + empty_chunks + dup_groups
        duration = time.time() - start

        await _complete_run(
            ctx,
            status="completed",
            source_table="kb_documents,kb_chunks",
            processed_rows=total_issues,
            affected_rows=0,
            error_rows=0,
            duration_seconds=duration,
            summary={
                "stale_documents": stale_docs,
                "empty_chunks": empty_chunks,
                "duplicate_chunk_groups": dup_groups,
                "total_issues": total_issues,
                "freshness_days": freshness_days,
            },
        )
    except Exception as e:
        duration = time.time() - start
        logger.exception("validate_kb_consistency 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table="kb_documents,kb_chunks",
            processed_rows=0,
            affected_rows=0,
            error_rows=0,
            duration_seconds=duration,
            error=str(e),
        )
        raise

    return {
        "stale_documents": stale_docs,
        "empty_chunks": empty_chunks,
        "duplicate_chunk_groups": dup_groups,
        "total_issues": total_issues,
    }


# ---------------------------------------------------------------------------
# 管道 5：质检标签清洗与合并
# ---------------------------------------------------------------------------


@task_handler("data_pipeline.clean_qa_labels", 1)
async def clean_qa_labels(ctx: TaskContext) -> Mapping[str, Any]:
    """合并低置信度的自动质检标签，标记需人工复核的条目。

    配置（config）：
      - confidence_threshold: 低置信度阈值，默认 0.6
      - dry_run: 仅统计不修改，默认 false
    """
    config = dict(ctx.payload.get("config", {}) or {})
    confidence_threshold = config.get("confidence_threshold", 0.6)
    dry_run = config.get("dry_run", False)

    start = time.time()
    low_count = 0
    updated = 0

    try:
        async with ctx.session_factory() as session:
            from sqlalchemy import text

            low_conf = (
                (
                    await session.execute(
                        text("""
                    SELECT id, thread_id, label, confidence, evidence
                    FROM qa_labels
                    WHERE source = 'auto' AND confidence < :threshold
                    ORDER BY thread_id, label
                """),
                        {"threshold": confidence_threshold},
                    )
                )
                .mappings()
                .all()
            )

            low_count = len(low_conf)
            if not dry_run:
                for label in low_conf:
                    evidence = dict(label.get("evidence", {}) or {})
                    evidence["needs_review"] = True
                    evidence["auto_confidence"] = label["confidence"]
                    await session.execute(
                        text("""
                            UPDATE qa_labels
                            SET evidence = :evidence::jsonb
                            WHERE id = :id
                        """),
                        {"id": label["id"], "evidence": evidence},
                    )
                    updated += 1
                await session.commit()

        duration = time.time() - start
        await _complete_run(
            ctx,
            status="completed",
            source_table="qa_labels",
            processed_rows=low_count,
            affected_rows=updated,
            error_rows=0,
            duration_seconds=duration,
            summary={
                "confidence_threshold": confidence_threshold,
                "low_confidence_labels": low_count,
                "marked_for_review": updated,
            },
        )
    except Exception as e:
        duration = time.time() - start
        logger.exception("clean_qa_labels 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table="qa_labels",
            processed_rows=low_count,
            affected_rows=updated,
            error_rows=0,
            duration_seconds=duration,
            error=str(e),
        )
        raise

    return {"low_confidence_labels": low_count, "marked_for_review": updated}


# ===================================================================
# 方案三：数据湖导出管道（PostgreSQL → Parquet → MinIO）
# ===================================================================


async def _latest_completed_summary(ctx: TaskContext, scope: str) -> dict[str, Any] | None:
    """读取同一 checkpoint scope 最近一次成功运行的 summary。

    checkpoint 的读取键与写入键统一为 run.summary 里的 `_scope`（见 `_complete_run`），
    与 `source_table` 解耦——此前用 task_type 查 source_table，键永不匹配导致增量失效。
    """
    from sqlalchemy import desc, select

    async with ctx.session_factory() as session:
        from app.models.data_pipeline import DataPipelineRun

        stmt = (
            select(DataPipelineRun.summary)
            .where(DataPipelineRun.summary[CHECKPOINT_SCOPE_KEY].astext == scope)
            .where(DataPipelineRun.status == "completed")
            .order_by(desc(DataPipelineRun.finished_at))
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


async def _get_checkpoint(ctx: TaskContext, scope: str) -> Any:
    """从上次成功运行的 summary 中读取 checkpoint（导出用；无则 None）。"""
    row = await _latest_completed_summary(ctx, scope)
    if row and row.get("checkpoint") is not None:
        return row["checkpoint"]
    return None


async def _get_checkpoints(ctx: TaskContext, scope: str) -> dict[str, Any]:
    """多表导出的分表 checkpoint：`{table_name: max_id}`。"""
    row = await _latest_completed_summary(ctx, scope)
    if row and isinstance(row.get("checkpoints"), dict):
        return dict(row["checkpoints"])
    return {}


async def _export_batch(
    ctx: TaskContext,
    *,
    table: str,
    columns: list[str],
    batch_size: int,
    checkpoint_col: str,
    checkpoint: int | None,
    date_col: str = "created_at",
    where_extra: str = "",
) -> dict[str, Any]:
    """通用增量导出：查询 PG 表，分批写入 MinIO Parquet。"""
    from datetime import datetime

    from sqlalchemy import text

    from app.data_pipeline.lake_storage import LakeStorage

    storage = LakeStorage.from_settings()
    total_rows = 0
    offset = 0
    col_list = ", ".join(columns)
    max_id = checkpoint or 0
    today = date.today()
    current_cp = checkpoint if checkpoint is not None else 0

    while True:
        where_clauses = [f"{date_col} < now()", f"{checkpoint_col} > :checkpoint"]
        if where_extra:
            where_clauses.append(where_extra)
        where_sql = " AND ".join(where_clauses)

        async with ctx.session_factory() as session:
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
                        {"limit": batch_size, "checkpoint": current_cp},
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
                # 转换 datetime/date 为 ISO 字符串（Parquet 兼容）
                for k, v in row_dict.items():
                    if isinstance(v, (datetime,)):
                        row_dict[k] = v.isoformat()
                    elif isinstance(v, date):
                        row_dict[k] = v.isoformat()
                batch.append(row_dict)
                current_id = row_dict.get(checkpoint_col, 0)
                if current_id and current_id > max_id:
                    max_id = current_id

            # 更新 checkpoint 使下一轮只导出未处理的行
            if max_id > current_cp:
                current_cp = max_id

            part_offset = offset // batch_size
            await storage.write_parquet(table, today, part_offset, batch)

            total_rows += len(batch)
            offset += len(batch)

            # 报告进度
            async with ctx.session_factory() as ps:
                from app.repositories.task_repository import TaskRepository

                repo = TaskRepository(ps)
                await repo.set_progress(
                    ctx.task_id,
                    owner_token=ctx.owner_token,
                    progress={"table": table, "exported": total_rows, "max_id": max_id},
                )
                await ps.commit()

    return {"table": table, "exported": total_rows, "max_id": max_id}


@task_handler("data_pipeline.export_messages", 1)
async def export_messages(ctx: TaskContext) -> Mapping[str, Any]:
    """导出 messages 表到数据湖 Parquet，增量基于 seq。"""
    config = dict(ctx.payload.get("config", {}) or {})
    batch_size = config.get("batch_size", 2000)
    checkpoint = await _get_checkpoint(ctx, "data_pipeline.export_messages")

    start = time.time()
    try:
        result = await _export_batch(
            ctx,
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
            batch_size=batch_size,
            checkpoint_col="seq",
            checkpoint=checkpoint,
        )
        duration = time.time() - start
        summary = {**result, "checkpoint": result["max_id"]}
        await _complete_run(
            ctx,
            status="completed",
            source_table="messages",
            processed_rows=result["exported"],
            affected_rows=0,
            error_rows=0,
            duration_seconds=duration,
            summary=summary,
            checkpoint_scope="data_pipeline.export_messages",
            checkpoint=result["max_id"],
        )
        return result
    except Exception as e:
        duration = time.time() - start
        logger.exception("export_messages 失败")
        await _complete_run(ctx, status="failed", error=str(e), duration_seconds=duration)
        raise


@task_handler("data_pipeline.export_audits", 1)
async def export_audits(ctx: TaskContext) -> Mapping[str, Any]:
    """导出 model_audit 和 tool_audit 到数据湖。"""
    config = dict(ctx.payload.get("config", {}) or {})
    batch_size = config.get("batch_size", 2000)

    start = time.time()
    total_rows = 0
    checkpoints: dict[str, Any] = await _get_checkpoints(ctx, "data_pipeline.export_audits")
    try:
        for table_name in ["model_audit", "tool_audit"]:
            checkpoint = checkpoints.get(table_name)
            extra = "deleted_at IS NULL"

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

            result = await _export_batch(
                ctx,
                table=table_name,
                columns=cols,
                batch_size=batch_size,
                checkpoint_col="id",
                checkpoint=checkpoint,
                where_extra=extra,
            )
            total_rows += result["exported"]
            checkpoints[table_name] = result["max_id"]

        duration = time.time() - start
        await _complete_run(
            ctx,
            status="completed",
            source_table="model_audit,tool_audit",
            processed_rows=total_rows,
            affected_rows=0,
            error_rows=0,
            duration_seconds=duration,
            summary={
                "exported": total_rows,
                "tables": ["model_audit", "tool_audit"],
                "checkpoints": checkpoints,
            },
            checkpoint_scope="data_pipeline.export_audits",
        )
        return {"exported": total_rows, "checkpoints": checkpoints}
    except Exception as e:
        duration = time.time() - start
        logger.exception("export_audits 失败")
        await _complete_run(
            ctx,
            status="failed",
            error=str(e),
            duration_seconds=duration,
            checkpoint_scope="data_pipeline.export_audits",
        )
        raise


# ===================================================================
# 业务数据导出：订单事实 + 房型/房间维度
# ===================================================================


async def _export_business_snapshot(
    ctx: TaskContext,
    *,
    table: str,
    query: str,
) -> int:
    """维度表全量快照：同一日期分区固定写到 part-000000，重复运行覆盖。"""
    from app.data_pipeline.lake_storage import LakeStorage

    async with ctx.session_factory() as session:
        from sqlalchemy import text

        rows = (await session.execute(text(query))).mappings().all()

    if not rows:
        return 0

    from datetime import datetime

    batch: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, value in item.items():
            if isinstance(value, datetime):
                item[key] = value.isoformat()
        batch.append(item)

    storage = LakeStorage.from_settings()
    await storage.write_parquet(table, date.today(), 0, batch)
    return len(batch)


async def _export_orders(ctx: TaskContext, *, batch_size: int) -> dict[str, Any]:
    """订单事实增量导出：按 (updated_at, id) 键集推进，重复版本由聚合端去重。"""
    from datetime import datetime

    from sqlalchemy import text

    from app.data_pipeline.lake_storage import LakeStorage

    storage = LakeStorage.from_settings()
    summary = await _latest_completed_summary(ctx, "data_pipeline.export_business")
    checkpoint = (summary or {}).get("checkpoint") or {}
    cp_updated_at = checkpoint.get("updated_at") if isinstance(checkpoint, dict) else None
    cp_id = checkpoint.get("id") if isinstance(checkpoint, dict) else None

    base_offset = await _next_part_offset(storage, "orders", date.today())
    total_rows = 0
    last_updated_at: str | None = cp_updated_at if isinstance(cp_updated_at, str) else None
    last_id: str | None = cp_id if isinstance(cp_id, str) else None

    while True:
        params: dict[str, Any] = {"limit": batch_size}
        where = ["updated_at < now()"]
        if last_updated_at and last_id:
            where.append("(updated_at, id) > (CAST(:cp_ts AS timestamptz), CAST(:cp_id AS uuid))")
            params["cp_ts"] = datetime.fromisoformat(last_updated_at)
            params["cp_id"] = last_id

        async with ctx.session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(f"""
                        SELECT
                            id::text AS id,
                            room_type_id::text AS room_type_id,
                            status,
                            check_in_date,
                            check_out_date,
                            nights,
                            amount_fen,
                            paid_amount_fen,
                            created_at,
                            updated_at
                        FROM orders
                        WHERE {" AND ".join(where)}
                        ORDER BY updated_at, id
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

        batch: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            last_updated_at = item["updated_at"].isoformat()
            last_id = item["id"]
            for key, value in item.items():
                if isinstance(value, (datetime, date)):
                    item[key] = value.isoformat()
            batch.append(item)

        part_offset = base_offset + total_rows // batch_size
        await storage.write_parquet("orders", date.today(), part_offset, batch)
        total_rows += len(batch)

        async with ctx.session_factory() as ps:
            from app.repositories.task_repository import TaskRepository

            repo = TaskRepository(ps)
            await repo.set_progress(
                ctx.task_id,
                owner_token=ctx.owner_token,
                progress={"table": "orders", "exported": total_rows},
            )
            await ps.commit()

    return {
        "exported": total_rows,
        "checkpoint": {"updated_at": last_updated_at, "id": last_id},
    }


async def _next_part_offset(storage: Any, table: str, day: date) -> int:
    """同一天多次导出时追加分区，避免覆盖上一次运行的文件。"""
    prefix = f"{table}/year={day.year}/month={day.month:02d}/day={day.day:02d}/part-"
    offsets: list[int] = []
    for path in await storage.list_partitions(table):
        if not path.startswith(prefix):
            continue
        try:
            offsets.append(int(path[len(prefix) :].split(".")[0]))
        except ValueError:
            continue
    return max(offsets, default=-1) + 1


@task_handler("data_pipeline.export_business", 1)
async def export_business(ctx: TaskContext) -> Mapping[str, Any]:
    """导出订单与房型/房间维度，供 DuckDB 生成房型月度入住率与客单价。

    配置（config）：
      - batch_size: 订单每批导出行数，默认 2000
    """
    config = dict(ctx.payload.get("config", {}) or {})
    batch_size = config.get("batch_size", 2000)

    start = time.time()
    try:
        room_types = await _export_business_snapshot(
            ctx,
            table="room_types",
            query="""
                SELECT
                    id::text AS id,
                    code,
                    name,
                    base_price_fen,
                    active,
                    updated_at
                FROM room_types
                ORDER BY code
            """,
        )
        rooms = await _export_business_snapshot(
            ctx,
            table="rooms",
            query="""
                SELECT
                    id::text AS id,
                    room_type_id::text AS room_type_id,
                    room_no,
                    status,
                    updated_at
                FROM rooms
                ORDER BY room_no
            """,
        )
        orders = await _export_orders(ctx, batch_size=batch_size)

        duration = time.time() - start
        await _complete_run(
            ctx,
            status="completed",
            source_table="orders",
            processed_rows=orders["exported"],
            affected_rows=0,
            error_rows=0,
            duration_seconds=duration,
            summary={
                "orders": orders["exported"],
                "room_types": room_types,
                "rooms": rooms,
                "checkpoint": orders["checkpoint"],
            },
            checkpoint_scope="data_pipeline.export_business",
            checkpoint=orders["checkpoint"],
        )
        return {
            "orders": orders["exported"],
            "room_types": room_types,
            "rooms": rooms,
        }
    except Exception as e:
        duration = time.time() - start
        logger.exception("export_business 失败")
        await _complete_run(
            ctx,
            status="failed",
            error=str(e),
            duration_seconds=duration,
            checkpoint_scope="data_pipeline.export_business",
        )
        raise


# ===================================================================
# DuckDB 预聚合管道：读取 Parquet 生成聚合报表
# ===================================================================


@task_handler("data_pipeline.build_aggregates", 1)
async def build_aggregates(ctx: TaskContext) -> Mapping[str, Any]:
    """使用 DuckDB 读取数据湖 Parquet，生成预聚合 Parquet。

    聚合内容：
      - 按日 token 消耗（model_audit）
      - 按日工具调用统计（tool_audit）
      - 房型月度入住率与客单价（orders + room_types + rooms）
    """
    import duckdb

    from app.data_pipeline.business_aggregates import (
        ROOM_TYPE_MONTHLY_TABLE,
        build_room_type_monthly,
    )
    from app.data_pipeline.lake_storage import LakeStorage

    storage = LakeStorage.from_settings()
    start = time.time()

    async def _download(files: list[str]) -> list[str]:
        return [str(await storage.read_parquet_to_temp(f)) for f in files]

    try:
        # 列出数据湖中现有 Parquet 文件
        audit_files = await storage.list_partitions("model_audit")
        tool_files = await storage.list_partitions("tool_audit")
        msg_files = await storage.list_partitions("messages")
        order_files = await storage.list_partitions("orders")
        room_type_files = await storage.list_partitions("room_types")
        room_files = await storage.list_partitions("rooms")

        if not any((audit_files, tool_files, msg_files, order_files)):
            await _complete_run(
                ctx,
                status="completed",
                source_table="agg",
                processed_rows=0,
                affected_rows=0,
                error_rows=0,
                duration_seconds=time.time() - start,
                summary={"status": "skipped", "reason": "no parquet files found"},
            )
            return {"status": "skipped", "reason": "no parquet files found"}

        # DuckDB 聚合
        con = duckdb.connect()
        try:
            # 注册审计文件为视图
            if audit_files:
                audit_local = await _download(audit_files)
                con.execute(f"""
                    CREATE VIEW model_audit_v AS
                    SELECT * FROM read_parquet({audit_local!s})
                """)
            if tool_files:
                tool_local = await _download(tool_files)
                con.execute(f"""
                    CREATE VIEW tool_audit_v AS
                    SELECT * FROM read_parquet({tool_local!s})
                """)

            results: dict[str, int] = {}

            # 聚合 1：按日 token 消耗
            if audit_files:
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
                    logger.info("token aggregation: %d rows", agg.num_rows)

            # 聚合 2：按日工具调用
            if tool_files:
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
                    await storage.write_parquet(
                        "agg_tool_daily", date.today(), 0, tool_agg.to_pylist()
                    )
                    results["tool_agg"] = tool_agg.num_rows

            # 聚合 3：房型月度入住率与客单价
            if order_files and room_type_files and room_files:
                agg = build_room_type_monthly(
                    con,
                    orders_paths=await _download(order_files),
                    room_types_paths=await _download(room_type_files),
                    rooms_paths=await _download(room_files),
                )
                if agg.num_rows > 0:
                    await storage.write_parquet(
                        ROOM_TYPE_MONTHLY_TABLE, date.today(), 0, agg.to_pylist()
                    )
                    results["room_type_monthly_agg"] = agg.num_rows
                    logger.info("room type monthly aggregation: %d rows", agg.num_rows)

            duration = time.time() - start
            await _complete_run(
                ctx,
                status="completed",
                source_table="agg",
                processed_rows=sum(results.values()),
                affected_rows=sum(results.values()),
                error_rows=0,
                duration_seconds=duration,
                summary={"status": "completed", "results": results},
            )
            return {"status": "completed", "results": results, "duration_seconds": duration}
        finally:
            con.close()
    except Exception as e:
        duration = time.time() - start
        logger.exception("build_aggregates 失败")
        await _complete_run(
            ctx,
            status="failed",
            source_table="agg",
            processed_rows=0,
            affected_rows=0,
            error_rows=1,
            duration_seconds=duration,
            error=str(e),
        )
        raise
