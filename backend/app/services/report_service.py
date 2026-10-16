"""运营报表聚合用例：一切数值由 PostgreSQL 审计事实只读派生（验收主张 A4）。

口径见术语表 §15：报表不落第二份统计副本，对同一时间窗，本服务返回的每个数值都等于
直接对审计表重算的结果；智能体日志的软删只影响日志可见性，报表按全量审计事实重算
（含软删行），删除中间数据不虚增、不虚减。

时间窗一律为半开区间 `[start, end)`，按事实行的 created_at 过滤（agent_runs 用 created_at）。

集成了 DuckDB 数据湖分析层：当数据湖中存在预聚合 Parquet 表（agg_token_daily、
agg_tool_daily）时，对 token 聚合、工具调用聚合等重查询走 DuckDB，否则降级回
PostgreSQL 直接聚合。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb
from sqlalchemy import String, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.audit import ModelAudit, ToolAudit
from app.models.conversation import RATING_DOWN, HandoffRequest, MessageFeedback
from app.models.eval import KbEvalReport
from app.models.runtime import AgentRun, Message, Thread
from app.models.task import Task
from app.repositories.agent_run_repository import TERMINAL_RUN_STATUSES

logger = logging.getLogger(__name__)

MODEL_STATUS_COMPLETED = "completed"
TOOL_STATUS_COMPLETED = "completed"
TASK_STATUSES_SUCCESS = "succeeded"
TASK_STATUSES_FAILED = "failed"

_DAY_FMT = "%Y-%m-%d"
# 热门问题降级口径的关键词切分：连续中日韩文字段或连续拉丁词，长度 ≥2 才计入
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[A-Za-z]{2,}")
_KEYWORD_STOPWORDS = frozenset(
    {"你们", "我们", "怎么", "什么", "可以", "这个", "那个", "请问", "一下", "现在", "the", "and"}
)


@dataclass(frozen=True)
class Reception:
    threads: int
    runs: int
    messages: int


@dataclass(frozen=True)
class Resolution:
    terminal_runs: int
    first_resolved_runs: int
    first_resolution_rate: float | None
    tasks_succeeded: int
    tasks_failed: int
    task_success_rate: float | None


@dataclass(frozen=True)
class HandoffRate:
    handoffs: int
    threads: int
    rate: float | None


@dataclass(frozen=True)
class Recall:
    offline_recall_at_k: float | None
    offline_k: int | None
    offline_report_id: int | None
    online_runs_with_retrieval: int
    online_runs_cited: int
    online_citation_rate: float | None


@dataclass(frozen=True)
class TokenTotals:
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int


@dataclass(frozen=True)
class TokenBucket:
    key: str
    prompt_tokens: int
    completion_tokens: int
    cached_prompt_tokens: int


@dataclass(frozen=True)
class CacheHit:
    prompt_tokens: int
    cached_prompt_tokens: int
    hit_rate: float | None
    estimated_saving: float


@dataclass(frozen=True)
class Performance:
    ttft_p50_ms: float | None
    ttft_p95_ms: float | None
    ttft_samples: int
    tool_calls: int
    tool_completed: int
    tool_success_rate: float | None
    runs_with_attempts: int
    runs_retried: int
    model_retry_rate: float | None


@dataclass(frozen=True)
class HotQuestions:
    top_tools: list[tuple[str, int]] = field(default_factory=list)
    top_keywords: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class DailyCount:
    day: date
    threads: int = 0
    runs: int = 0
    messages: int = 0


@dataclass(frozen=True)
class RunStatusDistribution:
    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0
    interrupted: int = 0
    interrupt_ratio: float | None = None


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def _as_int(value: Any) -> int:
    return int(value) if value is not None else 0


def _estimate_saving(cached_prompt_tokens: int) -> float:
    """缓存命中成本节省 = 命中 tokens × (标准输入单价 − 缓存输入单价)；单价未配置则为 0。"""
    settings = get_settings()
    delta_per_1k = settings.report_price_input_per_1k - settings.report_price_cached_input_per_1k
    if not delta_per_1k:
        return 0.0
    return round(cached_prompt_tokens / 1000.0 * delta_per_1k, 6)


def _window(column: Any, start: datetime, end: datetime) -> tuple[Any, Any]:
    return (column >= start, column < end)


class ReportService:
    """报表/健康面板聚合服务：所有查询只读，不写任何统计副本。

    对 token 消耗、工具调用等重聚合查询优先走 DuckDB 数据湖预聚合表，
    若预聚合表不存在则降级到 PostgreSQL 直接聚合。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # DuckDB 数据湖查询辅助
    # ------------------------------------------------------------------

    async def _duckdb_query_agg(self, agg_table: str, sql: str) -> list[dict[str, Any]] | None:
        """对数据湖中的预聚合 Parquet 表执行 DuckDB 查询。

        返回 [{col: value, ...}, ...] 或 None（表不存在、数据湖不可达或查询失败）。
        整个探测与读取过程都在兜底范围内：湖不可达时按契约降级，由调用方走 PostgreSQL。
        """
        from app.data_pipeline.lake_storage import LakeStorage

        try:
            storage = LakeStorage.from_settings()
            files = await storage.list_partitions(agg_table)
            if not files:
                return None

            local_paths = [str(await storage.read_parquet_to_temp(f)) for f in files]

            def _run() -> list[dict[str, Any]]:
                con = duckdb.connect()
                try:
                    con.execute(
                        f'CREATE OR REPLACE VIEW "{agg_table}" AS '
                        f"SELECT * FROM read_parquet({local_paths})"
                    )
                    rel = con.execute(sql)
                    columns = [desc[0] for desc in rel.description]
                    return [dict(zip(columns, row, strict=True)) for row in rel.fetchall()]
                finally:
                    con.close()

            return await asyncio.to_thread(_run)
        except Exception:
            logger.debug("DuckDB query on %s failed", agg_table, exc_info=True)
            return None

    async def reception(self, *, start: datetime, end: datetime) -> Reception:
        threads = await self._count(Thread, Thread.created_at, start, end)
        runs = await self._count(AgentRun, AgentRun.created_at, start, end)
        messages = await self._count(Message, Message.created_at, start, end)
        return Reception(threads=threads, runs=runs, messages=messages)

    async def reception_series(self, *, start: datetime, end: datetime) -> list[DailyCount]:
        days: dict[date, DailyCount] = {}

        def _bucket(day: date) -> DailyCount:
            return days.setdefault(day, DailyCount(day=day))

        for day, count in await self._daily(Thread, Thread.created_at, start, end):
            _bucket(day).threads = count
        for day, count in await self._daily(AgentRun, AgentRun.created_at, start, end):
            _bucket(day).runs = count
        for day, count in await self._daily(Message, Message.created_at, start, end):
            _bucket(day).messages = count
        return [days[day] for day in sorted(days)]

    async def resolution(self, *, start: datetime, end: datetime) -> Resolution:
        run_start, run_end = _window(AgentRun.created_at, start, end)
        terminal = list(TERMINAL_RUN_STATUSES)

        terminal_runs = (
            await self._session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(run_start, run_end, AgentRun.status.in_(terminal))
            )
        ).scalar_one()

        handoff_threads = select(HandoffRequest.thread_id).where(
            HandoffRequest.thread_id == AgentRun.thread_id
        )
        down_feedback = (
            select(MessageFeedback.id)
            .join(Message, Message.id == MessageFeedback.message_id)
            .where(
                Message.role == "assistant",
                Message.run_id == cast(AgentRun.id, String),
                MessageFeedback.rating == RATING_DOWN,
            )
        )
        first_resolved_runs = (
            await self._session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(
                    run_start,
                    run_end,
                    AgentRun.status == "completed",
                    ~handoff_threads.exists(),
                    ~down_feedback.exists(),
                )
            )
        ).scalar_one()

        task_start, task_end = _window(Task.created_at, start, end)
        tasks_succeeded = (
            await self._session.execute(
                select(func.count())
                .select_from(Task)
                .where(task_start, task_end, Task.status == TASK_STATUSES_SUCCESS)
            )
        ).scalar_one()
        tasks_failed = (
            await self._session.execute(
                select(func.count())
                .select_from(Task)
                .where(task_start, task_end, Task.status == TASK_STATUSES_FAILED)
            )
        ).scalar_one()

        return Resolution(
            terminal_runs=terminal_runs,
            first_resolved_runs=first_resolved_runs,
            first_resolution_rate=_rate(first_resolved_runs, terminal_runs),
            tasks_succeeded=tasks_succeeded,
            tasks_failed=tasks_failed,
            task_success_rate=_rate(tasks_succeeded, tasks_succeeded + tasks_failed),
        )

    async def handoff_rate(self, *, start: datetime, end: datetime) -> HandoffRate:
        handoffs = await self._count(HandoffRequest, HandoffRequest.created_at, start, end)
        threads = await self._count(Thread, Thread.created_at, start, end)
        return HandoffRate(handoffs=handoffs, threads=threads, rate=_rate(handoffs, threads))

    async def recall(self, *, start: datetime, end: datetime) -> Recall:
        latest = (
            await self._session.execute(
                select(KbEvalReport)
                .where(KbEvalReport.superseded_by.is_(None))
                .order_by(KbEvalReport.created_at.desc(), KbEvalReport.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        offline_recall: float | None = None
        offline_k: int | None = None
        offline_id: int | None = None
        if latest is not None:
            offline_id = latest.id
            offline_k = latest.k
            value = latest.metrics.get("recall_at_k")
            offline_recall = float(value) if isinstance(value, (int, float)) else None

        model_start, model_end = _window(ModelAudit.created_at, start, end)
        runs_with_retrieval = (
            await self._session.execute(
                select(func.count(func.distinct(ModelAudit.run_id))).where(
                    model_start, model_end, ModelAudit.status == MODEL_STATUS_COMPLETED
                )
            )
        ).scalar_one()
        runs_cited = (
            await self._session.execute(
                select(func.count(func.distinct(ModelAudit.run_id))).where(
                    model_start,
                    model_end,
                    ModelAudit.status == MODEL_STATUS_COMPLETED,
                    # 写入口径：retrieval_refs 只可能是非空数组或 None（None 落 JSONB null 标量）。
                    # jsonb_typeof 对任意 JSONB 值安全（含 null），'array' 即引用了检索片段。
                    func.jsonb_typeof(ModelAudit.retrieval_refs) == "array",
                )
            )
        ).scalar_one()
        return Recall(
            offline_recall_at_k=offline_recall,
            offline_k=offline_k,
            offline_report_id=offline_id,
            online_runs_with_retrieval=runs_with_retrieval,
            online_runs_cited=runs_cited,
            online_citation_rate=_rate(runs_cited, runs_with_retrieval),
        )

    async def token_totals(self, *, start: datetime, end: datetime) -> TokenTotals:
        # DuckDB 优先：agg_token_daily 存在时走预聚合查询
        rows = await self._duckdb_query_agg(
            "agg_token_daily",
            f"""
                SELECT
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(completion_tokens), 0) AS ct,
                    COALESCE(SUM(cached_tokens), 0) AS cpt
                FROM "agg_token_daily"
                WHERE day >= '{start.date()}' AND day < '{end.date()}'
            """,
        )
        if rows is not None:
            r = rows[0]
            return TokenTotals(
                prompt_tokens=int(r["pt"]),
                completion_tokens=int(r["ct"]),
                cached_prompt_tokens=int(r["cpt"]),
            )

        # 降级：PostgreSQL 直接聚合
        model_start, model_end = _window(ModelAudit.created_at, start, end)
        row = (
            await self._session.execute(
                select(
                    func.coalesce(func.sum(ModelAudit.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelAudit.completion_tokens), 0),
                    func.coalesce(func.sum(ModelAudit.cached_prompt_tokens), 0),
                ).where(model_start, model_end)
            )
        ).one()
        return TokenTotals(
            prompt_tokens=_as_int(row[0]),
            completion_tokens=_as_int(row[1]),
            cached_prompt_tokens=_as_int(row[2]),
        )

    async def token_usage_by_model(self, *, start: datetime, end: datetime) -> list[TokenBucket]:
        rows = await self._duckdb_query_agg(
            "agg_token_daily",
            f"""
                SELECT
                    model AS bucket,
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(completion_tokens), 0) AS ct,
                    COALESCE(SUM(cached_tokens), 0) AS cpt
                FROM "agg_token_daily"
                WHERE day >= '{start.date()}' AND day < '{end.date()}'
                GROUP BY model
                ORDER BY model
            """,
        )
        if rows is not None:
            return [
                TokenBucket(
                    key=r["bucket"],
                    prompt_tokens=int(r["pt"]),
                    completion_tokens=int(r["ct"]),
                    cached_prompt_tokens=int(r["cpt"]),
                )
                for r in rows
            ]
        return await self._token_group(ModelAudit.model, start, end)

    async def token_usage_by_agent(self, *, start: datetime, end: datetime) -> list[TokenBucket]:
        return await self._token_group(ModelAudit.agent_slug, start, end)

    async def token_usage_by_day(self, *, start: datetime, end: datetime) -> list[TokenBucket]:
        rows = await self._duckdb_query_agg(
            "agg_token_daily",
            f"""
                SELECT
                    CAST(day AS VARCHAR) AS bucket,
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(completion_tokens), 0) AS ct,
                    COALESCE(SUM(cached_tokens), 0) AS cpt
                FROM "agg_token_daily"
                WHERE day >= '{start.date()}' AND day < '{end.date()}'
                GROUP BY day
                ORDER BY day
            """,
        )
        if rows is not None:
            return [
                TokenBucket(
                    key=r["bucket"],
                    prompt_tokens=int(r["pt"]),
                    completion_tokens=int(r["ct"]),
                    cached_prompt_tokens=int(r["cpt"]),
                )
                for r in rows
            ]
        day = func.to_char(ModelAudit.created_at, _DAY_FMT)
        return await self._token_group(day, start, end)

    async def cache_hit(self, *, start: datetime, end: datetime) -> CacheHit:
        totals = await self.token_totals(start=start, end=end)
        return CacheHit(
            prompt_tokens=totals.prompt_tokens,
            cached_prompt_tokens=totals.cached_prompt_tokens,
            hit_rate=_rate(totals.cached_prompt_tokens, totals.prompt_tokens),
            estimated_saving=_estimate_saving(totals.cached_prompt_tokens),
        )

    async def cache_hit_series(self, *, start: datetime, end: datetime) -> list[CacheHit]:
        rows = await self._duckdb_query_agg(
            "agg_token_daily",
            f"""
                SELECT
                    CAST(day AS VARCHAR) AS bucket,
                    COALESCE(SUM(prompt_tokens), 0) AS pt,
                    COALESCE(SUM(cached_tokens), 0) AS cpt
                FROM "agg_token_daily"
                WHERE day >= '{start.date()}' AND day < '{end.date()}'
                GROUP BY day
                ORDER BY day
            """,
        )
        if rows is not None:
            series: list[CacheHit] = []
            for r in rows:
                pt_i = int(r["pt"])
                cpt_i = int(r["cpt"])
                series.append(
                    CacheHit(
                        prompt_tokens=pt_i,
                        cached_prompt_tokens=cpt_i,
                        hit_rate=_rate(cpt_i, pt_i),
                        estimated_saving=_estimate_saving(cpt_i),
                    )
                )
            return series

        day = func.to_char(ModelAudit.created_at, _DAY_FMT)
        model_start, model_end = _window(ModelAudit.created_at, start, end)
        pg_rows = (
            await self._session.execute(
                select(
                    day.label("bucket"),
                    func.coalesce(func.sum(ModelAudit.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelAudit.cached_prompt_tokens), 0),
                )
                .where(model_start, model_end)
                .group_by(day)
                .order_by(day)
            )
        ).all()
        series_pg: list[CacheHit] = []
        for _bucket, prompt, cached in pg_rows:
            prompt_i, cached_i = _as_int(prompt), _as_int(cached)
            series_pg.append(
                CacheHit(
                    prompt_tokens=prompt_i,
                    cached_prompt_tokens=cached_i,
                    hit_rate=_rate(cached_i, prompt_i),
                    estimated_saving=_estimate_saving(cached_i),
                )
            )
        return series_pg

    async def performance(self, *, start: datetime, end: datetime) -> Performance:
        ttft_p50, ttft_p95, ttft_samples = await self._ttft_percentiles(start, end)

        # DuckDB 优先：agg_tool_daily 存在时查预聚合表
        tool_rows = await self._duckdb_query_agg(
            "agg_tool_daily",
            f"""
                SELECT
                    COALESCE(SUM(calls), 0) AS total_calls,
                    COALESCE(
                        SUM(CASE WHEN status = '{TOOL_STATUS_COMPLETED}' THEN calls ELSE 0 END), 0
                    ) AS completed_calls
                FROM "agg_tool_daily"
                WHERE day >= '{start.date()}' AND day < '{end.date()}'
            """,
        )
        if tool_rows is not None:
            r = tool_rows[0]
            tool_calls = int(r["total_calls"])
            tool_completed = int(r["completed_calls"])
        else:
            # 降级：PostgreSQL 直接聚合
            tool_start, tool_end = _window(ToolAudit.created_at, start, end)
            tool_calls = (
                await self._session.execute(
                    select(func.count()).select_from(ToolAudit).where(tool_start, tool_end)
                )
            ).scalar_one()
            tool_completed = (
                await self._session.execute(
                    select(func.count())
                    .select_from(ToolAudit)
                    .where(tool_start, tool_end, ToolAudit.status == TOOL_STATUS_COMPLETED)
                )
            ).scalar_one()

        run_start, run_end = _window(AgentRun.created_at, start, end)
        runs_with_attempts = (
            await self._session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(run_start, run_end, AgentRun.attempt_count >= 1)
            )
        ).scalar_one()
        runs_retried = (
            await self._session.execute(
                select(func.count())
                .select_from(AgentRun)
                .where(run_start, run_end, AgentRun.attempt_count > 1)
            )
        ).scalar_one()

        return Performance(
            ttft_p50_ms=ttft_p50,
            ttft_p95_ms=ttft_p95,
            ttft_samples=ttft_samples,
            tool_calls=tool_calls,
            tool_completed=tool_completed,
            tool_success_rate=_rate(tool_completed, tool_calls),
            runs_with_attempts=runs_with_attempts,
            runs_retried=runs_retried,
            model_retry_rate=_rate(runs_retried, runs_with_attempts),
        )

    async def hot_questions(
        self, *, start: datetime, end: datetime, limit: int = 10
    ) -> HotQuestions:
        tool_start, tool_end = _window(ToolAudit.created_at, start, end)
        tool_rows = (
            await self._session.execute(
                select(ToolAudit.tool_name, func.count())
                .where(tool_start, tool_end)
                .group_by(ToolAudit.tool_name)
                .order_by(func.count().desc(), ToolAudit.tool_name)
                .limit(limit)
            )
        ).all()
        top_tools = [(str(name), int(count)) for name, count in tool_rows]

        msg_start, msg_end = _window(Message.created_at, start, end)
        # 每线程首条客人消息：row_number 窗口取 rn=1（避免 DISTINCT ON 的方言耦合）
        ranked = (
            select(
                Message.content.label("content"),
                func.row_number()
                .over(partition_by=Message.thread_id, order_by=Message.created_at)
                .label("rn"),
            )
            .where(Message.role == "user", msg_start, msg_end)
            .subquery()
        )
        first_messages = (
            await self._session.execute(select(ranked.c.content).where(ranked.c.rn == 1))
        ).all()
        counter: Counter[str] = Counter()
        for (content,) in first_messages:
            text = content.get("text") if isinstance(content, dict) else None
            if not isinstance(text, str):
                continue
            for token in _TOKEN_RE.findall(text):
                if token.lower() in _KEYWORD_STOPWORDS:
                    continue
                counter[token] += 1
        top_keywords = counter.most_common(limit)
        return HotQuestions(top_tools=top_tools, top_keywords=top_keywords)

    async def run_status_distribution(
        self, *, start: datetime, end: datetime
    ) -> RunStatusDistribution:
        run_start, run_end = _window(AgentRun.created_at, start, end)
        rows = (
            await self._session.execute(
                select(AgentRun.status, func.count())
                .where(run_start, run_end)
                .group_by(AgentRun.status)
            )
        ).all()
        counts = {str(status): int(count) for status, count in rows}
        total = sum(counts.values())
        interrupted = counts.get("interrupted", 0)
        return RunStatusDistribution(
            counts=counts,
            total=total,
            interrupted=interrupted,
            interrupt_ratio=_rate(interrupted, total),
        )

    async def _token_group(self, column: Any, start: datetime, end: datetime) -> list[TokenBucket]:
        model_start, model_end = _window(ModelAudit.created_at, start, end)
        rows = (
            await self._session.execute(
                select(
                    column.label("bucket"),
                    func.coalesce(func.sum(ModelAudit.prompt_tokens), 0),
                    func.coalesce(func.sum(ModelAudit.completion_tokens), 0),
                    func.coalesce(func.sum(ModelAudit.cached_prompt_tokens), 0),
                )
                .where(model_start, model_end)
                .group_by(column)
                .order_by(column)
            )
        ).all()
        return [
            TokenBucket(
                key=str(bucket),
                prompt_tokens=_as_int(prompt),
                completion_tokens=_as_int(completion),
                cached_prompt_tokens=_as_int(cached),
            )
            for bucket, prompt, completion, cached in rows
        ]

    async def _ttft_percentiles(
        self, start: datetime, end: datetime
    ) -> tuple[float | None, float | None, int]:
        run_start, run_end = _window(AgentRun.created_at, start, end)
        elapsed_ms = func.extract("epoch", AgentRun.first_token_at - AgentRun.started_at) * 1000.0
        base: list[Any] = [
            run_start,
            run_end,
            AgentRun.first_token_at.is_not(None),
            AgentRun.started_at.is_not(None),
        ]
        samples = (
            await self._session.execute(select(func.count()).select_from(AgentRun).where(*base))
        ).scalar_one()
        if samples == 0:
            return None, None, 0
        row = (
            await self._session.execute(
                select(
                    func.percentile_cont(0.5).within_group(elapsed_ms),
                    func.percentile_cont(0.95).within_group(elapsed_ms),
                ).where(*base)
            )
        ).one()
        p50 = float(row[0]) if row[0] is not None else None
        p95 = float(row[1]) if row[1] is not None else None
        return (
            round(p50, 3) if p50 is not None else None,
            round(p95, 3) if p95 is not None else None,
            int(samples),
        )

    async def _count(self, model: type[Any], column: Any, start: datetime, end: datetime) -> int:
        cond_start, cond_end = _window(column, start, end)
        return (
            await self._session.execute(
                select(func.count()).select_from(model).where(cond_start, cond_end)
            )
        ).scalar_one()

    async def _daily(
        self, model: type[Any], column: Any, start: datetime, end: datetime
    ) -> list[tuple[date, int]]:
        day = func.date_trunc("day", column)
        cond_start, cond_end = _window(column, start, end)
        rows = (
            await self._session.execute(
                select(day.label("bucket"), func.count())
                .select_from(model)
                .where(cond_start, cond_end)
                .group_by(day)
                .order_by(day)
            )
        ).all()
        return [
            (bucket.date() if isinstance(bucket, datetime) else bucket, _as_int(count))
            for bucket, count in rows
        ]


__all__ = [
    "CacheHit",
    "DailyCount",
    "HandoffRate",
    "HotQuestions",
    "Performance",
    "Reception",
    "Recall",
    "ReportService",
    "Resolution",
    "RunStatusDistribution",
    "TokenBucket",
    "TokenTotals",
]
