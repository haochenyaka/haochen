"""告警规则评估：只读 PostgreSQL 事实与进程内连接池，不落第二份统计副本。

规则（阈值全部来自配置）：
- run_stuck：pending 超时未派发，或 running/cancel_requested 心跳停滞/租约过期；
- lease_expiry_rate：窗口内已结束尝试中 lease_expired 占比；
- sse_disconnect：窗口内 SSE 异常终止率，或事件总线跟随/回放失败（降级）；
- db_pool_exhaustion：本进程连接池签出率。

同一时间窗可随时重算；返回值携带事实明细，供面板与 on-call 直接定位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.runtime import AgentRun, AgentRunAttempt
from app.services.observability_service import (
    NullSseMetrics,
    PoolStats,
    SseMetrics,
    database_pool_stats,
)

_UNSET = object()

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_CRITICAL = "critical"

ACTIVE_RUN_STATUSES: tuple[str, ...] = ("running", "cancel_requested")


@dataclass(frozen=True)
class AlertRule:
    rule: str
    severity: str
    triggered: bool
    value: float | None
    threshold: float | None
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertReport:
    status: str
    evaluated_at: datetime
    rules: tuple[AlertRule, ...]


class AlertService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        metrics: SseMetrics | None = None,
        pool_stats: PoolStats | None | object = _UNSET,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._metrics = metrics or NullSseMetrics()
        # 省略时读取本进程池状态；显式传 None 表示池统计不可用（测试与无池部署）
        self._pool_stats = database_pool_stats() if pool_stats is _UNSET else pool_stats
        self._settings = settings or get_settings()

    async def evaluate(self, *, now: datetime | None = None) -> AlertReport:
        moment = now or datetime.now(UTC)
        rules = (
            await self._run_stuck(moment),
            await self._lease_expiry_rate(moment),
            await self._sse_disconnect(),
            self._db_pool_exhaustion(),
        )
        return AlertReport(status=_overall_status(rules), evaluated_at=moment, rules=rules)

    async def _run_stuck(self, now: datetime) -> AlertRule:
        settings = self._settings
        pending_cutoff = now - timedelta(seconds=settings.alert_pending_stuck_seconds)
        heartbeat_cutoff = now - timedelta(seconds=settings.alert_running_stuck_seconds)

        pending_stuck = await self._scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.status == "pending",
                AgentRun.created_at < pending_cutoff,
            )
        )
        running_stuck = await self._scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                or_(
                    AgentRun.last_heartbeat_at.is_(None),
                    AgentRun.last_heartbeat_at < heartbeat_cutoff,
                ),
            )
        )
        expired_leases = await self._scalar(
            select(func.count())
            .select_from(AgentRun)
            .where(
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
                AgentRun.lease_expires_at.is_not(None),
                AgentRun.lease_expires_at < now,
            )
        )

        total = pending_stuck + running_stuck
        triggered = total > 0
        severity = SEVERITY_CRITICAL if running_stuck > 0 else SEVERITY_WARNING
        summary = (
            f"卡死 Run {total} 个：pending 超时 {pending_stuck} 个"
            f"（>{settings.alert_pending_stuck_seconds}s），"
            f"运行中心跳停滞 {running_stuck} 个"
            f"（>{settings.alert_running_stuck_seconds}s），"
            f"其中租约已过期 {expired_leases} 个"
        )
        return AlertRule(
            rule="run_stuck",
            severity=severity,
            triggered=triggered,
            value=total,
            threshold=0,
            summary=summary,
            facts={
                "pending_stuck": pending_stuck,
                "running_stuck": running_stuck,
                "expired_leases": expired_leases,
                "pending_threshold_seconds": settings.alert_pending_stuck_seconds,
                "running_threshold_seconds": settings.alert_running_stuck_seconds,
            },
        )

    async def _lease_expiry_rate(self, now: datetime) -> AlertRule:
        settings = self._settings
        window_start = now - timedelta(minutes=settings.alert_lease_window_minutes)
        window = (
            select(func.count())
            .select_from(AgentRunAttempt)
            .where(
                AgentRunAttempt.started_at >= window_start,
                AgentRunAttempt.finished_at.is_not(None),
            )
        )
        attempts = await self._scalar(window)
        expired = await self._scalar(window.where(AgentRunAttempt.outcome == "lease_expired"))
        rate = expired / attempts if attempts > 0 else None
        enough_samples = attempts >= settings.alert_lease_min_attempts
        triggered = enough_samples and rate is not None and rate > settings.alert_lease_expiry_rate
        if attempts == 0:
            summary = "窗口内无可评估的尝试事实"
        elif not enough_samples:
            summary = (
                f"窗口内已结束尝试 {attempts} 个，少于最小样本 "
                f"{settings.alert_lease_min_attempts}，不判定"
            )
        else:
            summary = (
                f"租约过期率 {rate:.2%}（{expired}/{attempts}），"
                f"阈值 {settings.alert_lease_expiry_rate:.2%}"
            )
        return AlertRule(
            rule="lease_expiry_rate",
            severity=SEVERITY_CRITICAL,
            triggered=triggered,
            value=rate,
            threshold=settings.alert_lease_expiry_rate,
            summary=summary,
            facts={
                "attempts": attempts,
                "lease_expired": expired,
                "window_minutes": settings.alert_lease_window_minutes,
                "min_attempts": settings.alert_lease_min_attempts,
                "enough_samples": enough_samples,
            },
        )

    async def _sse_disconnect(self) -> AlertRule:
        settings = self._settings
        snapshot = await self._metrics.snapshot(window_minutes=settings.alert_sse_window_minutes)
        rate = snapshot.disconnect_rate
        enough_samples = snapshot.started >= settings.alert_sse_min_streams
        storage_degraded = snapshot.storage_errors > 0
        rate_triggered = (
            enough_samples and rate is not None and rate > settings.alert_sse_disconnect_rate
        )
        triggered = storage_degraded or rate_triggered
        if not enough_samples and not storage_degraded:
            summary = (
                f"窗口内订阅 {snapshot.started} 次，少于最小样本 "
                f"{settings.alert_sse_min_streams}，不判定断流率"
            )
        else:
            summary = f"SSE 异常收流率 {rate:.2%}" if rate is not None else "SSE 无订阅事实"
            summary += (
                f"（异常终止 {snapshot.abnormal}/{snapshot.started}；"
                f"客户端断开 {snapshot.client_disconnects}、"
                f"服务端异常 {snapshot.stream_errors}；"
                f"总线跟随失败 {snapshot.follow_errors}、回放失败 {snapshot.read_errors}）"
            )
            if storage_degraded:
                summary += "；事件总线读取失败，已降级为数据库投影"
        return AlertRule(
            rule="sse_disconnect",
            severity=SEVERITY_WARNING,
            triggered=triggered,
            value=rate,
            threshold=settings.alert_sse_disconnect_rate,
            summary=summary,
            facts={
                "streams_started": snapshot.started,
                "streams_completed": snapshot.completed,
                "client_disconnects": snapshot.client_disconnects,
                "stream_errors": snapshot.stream_errors,
                "follow_errors": snapshot.follow_errors,
                "read_errors": snapshot.read_errors,
                "abnormal": snapshot.abnormal,
                "storage_errors": snapshot.storage_errors,
                "window_minutes": settings.alert_sse_window_minutes,
                "min_streams": settings.alert_sse_min_streams,
                "storage_degraded": storage_degraded,
            },
        )

    def _db_pool_exhaustion(self) -> AlertRule:
        threshold = self._settings.alert_db_pool_usage
        stats = self._pool_stats
        if not isinstance(stats, PoolStats) or stats.utilization is None:
            return AlertRule(
                rule="db_pool_exhaustion",
                severity=SEVERITY_CRITICAL,
                triggered=False,
                value=None,
                threshold=threshold,
                summary="连接池统计不可用（池实现不支持或未初始化）",
                facts={"available": False},
            )
        utilization = stats.utilization
        triggered = utilization >= threshold
        return AlertRule(
            rule="db_pool_exhaustion",
            severity=SEVERITY_CRITICAL,
            triggered=triggered,
            value=utilization,
            threshold=threshold,
            summary=(
                f"连接池签出率 {utilization:.1%}（{stats.checked_out}/{stats.size}），"
                f"阈值 {threshold:.1%}；溢出连接 {stats.overflow}"
            ),
            facts={
                "available": True,
                "pool_size": stats.size,
                "checked_out": stats.checked_out,
                "overflow": stats.overflow,
            },
        )

    async def _scalar(self, stmt: Any) -> int:
        value = await self._session.scalar(stmt)
        return int(value or 0)


def _overall_status(rules: tuple[AlertRule, ...]) -> str:
    firing = [rule for rule in rules if rule.triggered]
    if any(rule.severity == SEVERITY_CRITICAL for rule in firing):
        return STATUS_CRITICAL
    if firing:
        return STATUS_WARNING
    return STATUS_OK
