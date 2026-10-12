"""报表聚合服务验证：每个指标 == 直接对审计事实重算（验收主张 A4 核心取证）。

种子事实手工构造，断言值即对这些事实的独立重算；软删只影响日志可见性、报表按全量
审计重算的口径由 test_admin_reports_api 覆盖。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ModelAudit, ToolAudit
from app.models.conversation import HandoffRequest, MessageFeedback
from app.models.runtime import AgentRun, Message, Thread
from app.models.task import Task
from app.repositories import AgentRunRepository, HandoffRequestRepository, MessageRepository
from app.services.report_service import ReportService
from tests.integration.db_utils import run_with_session
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration


def _unique_window() -> tuple[datetime, datetime, datetime]:
    """专属时间窗：种子事实全部落在 2031 年的唯一窗口内，规避共享库其它测试的 now() 数据污染。"""
    base = datetime(2031, 1, 1, tzinfo=UTC) + timedelta(seconds=uuid4().int % (300 * 86400))
    return base, base + timedelta(minutes=10), base + timedelta(minutes=1)


async def _stamp(session: AsyncSession, thread_ids: list[UUID], stamp: datetime) -> None:
    """把接力路径落库的线程/消息/运行/转人工行的 created_at 统一改写到专属窗口。"""
    await session.execute(
        update(Thread).where(Thread.id.in_(thread_ids)).values(created_at=stamp)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Message).where(Message.thread_id.in_(thread_ids)).values(created_at=stamp)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(AgentRun).where(AgentRun.thread_id.in_(thread_ids)).values(created_at=stamp)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(HandoffRequest)
        .where(HandoffRequest.thread_id.in_(thread_ids))
        .values(created_at=stamp)
        .execution_options(synchronize_session=False)
    )


async def _seed(session: AsyncSession) -> dict[str, Any]:
    """构造三线程/三 Run + 审计事实 + 反馈/转人工/任务，返回关键 id 与专属时间窗。"""
    start, end, stamp = _unique_window()

    a = await intake_message(session, username="report-a", text="房间WiFi怎么用")
    b = await intake_message(session, username="report-b", text="我要取消订单")
    c = await intake_message(session, username="report-c", text="退房流程是什么")
    assert a.run_id and b.run_id and c.run_id

    runs = AgentRunRepository(session)

    # Run A：completed、有首 token、单次尝试、模型引用知识库、工具 1 成功 1 失败、无负反馈
    run_a = await runs.get_by_id(a.run_id)
    assert run_a is not None
    run_a.status = "completed"
    run_a.attempt_count = 1
    run_a.started_at = stamp
    run_a.first_token_at = stamp + timedelta(seconds=1)
    msg_a = await MessageRepository(session).append(
        thread_id=a.thread_id,
        role="assistant",
        content={"text": "WiFi 密码在房卡套"},
        run_id=str(a.run_id),
    )
    session.add(
        ModelAudit(
            run_id=str(a.run_id), seq=1, agent_slug="front-desk", actor_type="system",
            actor_id="t", model="deepseek-flash", status="completed", duration_ms=900,
            prompt_tokens=100, cached_prompt_tokens=40, completion_tokens=20,
            retrieval_refs=[{"chunk_id": "c1"}], created_at=stamp,
        )
    )
    session.add(
        ToolAudit(
            run_id=str(a.run_id), seq=1, agent_slug="front-desk", tool_name="query_kb",
            status="completed", duration_ms=30, created_at=stamp,
        )
    )
    session.add(
        ToolAudit(
            run_id=str(a.run_id), seq=2, agent_slug="front-desk", tool_name="list_my_orders",
            status="failed", duration_ms=12, error="boom", created_at=stamp,
        )
    )

    # Run B：completed、有首 token、单次尝试、模型无引用、工具 1 成功、助手消息被点踩
    run_b = await runs.get_by_id(b.run_id)
    assert run_b is not None
    run_b.status = "completed"
    run_b.attempt_count = 1
    run_b.started_at = stamp
    run_b.first_token_at = stamp + timedelta(seconds=2)
    msg_b = await MessageRepository(session).append(
        thread_id=b.thread_id,
        role="assistant",
        content={"text": "已为您取消"},
        run_id=str(b.run_id),
    )
    session.add(
        ModelAudit(
            run_id=str(b.run_id), seq=1, agent_slug="front-desk", actor_type="system",
            actor_id="t", model="deepseek-flash", status="completed", duration_ms=500,
            prompt_tokens=200, cached_prompt_tokens=0, completion_tokens=50, retrieval_refs=None,
            created_at=stamp,
        )
    )
    session.add(
        ToolAudit(
            run_id=str(b.run_id), seq=1, agent_slug="front-desk", tool_name="cancel_order",
            status="completed", duration_ms=40, created_at=stamp,
        )
    )
    session.add(
        MessageFeedback(message_id=msg_b.id, user_id=run_b.user_id, rating="down")
    )
    await HandoffRequestRepository(session).create(
        thread_id=b.thread_id, user_id=run_b.user_id, reason="客人要求人工"
    )

    # Run C：failed、无首 token、两次尝试（重试）；无模型/工具审计
    run_c = await runs.get_by_id(c.run_id)
    assert run_c is not None
    run_c.status = "failed"
    run_c.attempt_count = 2
    run_c.started_at = stamp
    run_c.first_token_at = None

    # Durable Task：1 成功 1 失败 → 任务成功率 0.5
    session.add(
        Task(
            task_type="lock.rotate_daily", handler_version=1, payload={}, status="succeeded",
            created_at=stamp,
        )
    )
    session.add(
        Task(
            task_type="checkin.prepare", handler_version=1, payload={}, status="failed",
            created_at=stamp,
        )
    )

    await _stamp(session, [a.thread_id, b.thread_id, c.thread_id], stamp)
    await session.commit()
    return {"start": start, "end": end, "msg_a": msg_a.id}



def test_report_metrics_match_audit_recompute(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        seeded = await _seed(session)
        start, end = seeded["start"], seeded["end"]
        svc = ReportService(session)

        reception = await svc.reception(start=start, end=end)
        resolution = await svc.resolution(start=start, end=end)
        handoff = await svc.handoff_rate(start=start, end=end)
        recall = await svc.recall(start=start, end=end)
        tokens = await svc.token_totals(start=start, end=end)
        cache = await svc.cache_hit(start=start, end=end)
        perf = await svc.performance(start=start, end=end)
        dist = await svc.run_status_distribution(start=start, end=end)
        hot = await svc.hot_questions(start=start, end=end, limit=5)
        by_model = await svc.token_usage_by_model(start=start, end=end)

        return {
            "reception": reception,
            "resolution": resolution,
            "handoff": handoff,
            "recall": recall,
            "tokens": tokens,
            "cache": cache,
            "perf": perf,
            "dist": dist,
            "hot": hot,
            "by_model": by_model,
        }

    r = run_with_session(runtime_db_url, _body)

    # 接待量：3 线程 3 Run（消息数含用户+助手，不断言精确值以免脆弱）
    assert r["reception"].threads == 3
    assert r["reception"].runs == 3
    assert r["reception"].messages >= 3

    # 解决率：终态 Run 3（A/B completed + C failed）；一次解决只有 A（B 被点踩且转人工）
    res = r["resolution"]
    assert res.terminal_runs == 3
    assert res.first_resolved_runs == 1
    assert res.first_resolution_rate == round(1 / 3, 6)
    assert res.tasks_succeeded == 1 and res.tasks_failed == 1
    assert res.task_success_rate == 0.5

    # 转人工率：1 handoff / 3 线程
    assert r["handoff"].handoffs == 1
    assert r["handoff"].threads == 3
    assert r["handoff"].rate == round(1 / 3, 6)

    # 召回率：线上有检索行为的 Run = 有 completed 模型审计的 A/B = 2；被引用 = A = 1
    rec = r["recall"]
    assert rec.offline_recall_at_k is None  # 无 kb_eval 报告
    assert rec.online_runs_with_retrieval == 2
    assert rec.online_runs_cited == 1
    assert rec.online_citation_rate == 0.5

    # token 消耗：prompt 100+200=300、cached 40、completion 20+50=70
    assert r["tokens"].prompt_tokens == 300
    assert r["tokens"].cached_prompt_tokens == 40
    assert r["tokens"].completion_tokens == 70
    assert r["cache"].hit_rate == round(40 / 300, 6)
    assert r["cache"].estimated_saving == 0.0  # 单价未配置

    # 运行性能：TTFT 样本 = 有首 token 的 A/B = 2；工具 3 调用 2 成功；重试 = C（attempt>1）
    perf = r["perf"]
    assert perf.ttft_samples == 2
    assert perf.ttft_p50_ms is not None and perf.ttft_p95_ms is not None
    assert perf.tool_calls == 3 and perf.tool_completed == 2
    assert perf.tool_success_rate == round(2 / 3, 6)
    assert perf.runs_with_attempts == 3 and perf.runs_retried == 1
    assert perf.model_retry_rate == round(1 / 3, 6)

    # Run 终态分布：completed 2、failed 1、总 3、interrupted 0
    dist = r["dist"]
    assert dist.counts.get("completed") == 2
    assert dist.counts.get("failed") == 1
    assert dist.total == 3
    assert dist.interrupted == 0 and dist.interrupt_ratio == 0.0

    # 热门问题降级口径：工具 TOP 含 query_kb/cancel_order；关键词来自首条客人消息
    tool_names = {name for name, _ in r["hot"].top_tools}
    assert {"query_kb", "cancel_order", "list_my_orders"} <= tool_names
    assert r["hot"].top_keywords  # 非空即可（分词近似）

    # token 按模型分组：单一模型 deepseek-flash 汇总 300/70/40
    assert len(r["by_model"]) == 1
    assert r["by_model"][0].key == "deepseek-flash"
    assert r["by_model"][0].prompt_tokens == 300


def test_report_empty_window_returns_zero_and_none_rates(runtime_db_url: str) -> None:
    async def _body(session: AsyncSession) -> dict[str, Any]:
        await _seed(session)
        # 专属空窗：2033 年无任何事实落入（与种子的 2031 窗口互不重叠）
        start = datetime(2033, 1, 1, tzinfo=UTC)
        end = datetime(2033, 1, 2, tzinfo=UTC)
        svc = ReportService(session)
        return {
            "reception": await svc.reception(start=start, end=end),
            "resolution": await svc.resolution(start=start, end=end),
            "recall": await svc.recall(start=start, end=end),
            "perf": await svc.performance(start=start, end=end),
            "dist": await svc.run_status_distribution(start=start, end=end),
        }

    r = run_with_session(runtime_db_url, _body)
    assert r["reception"].threads == 0 and r["reception"].runs == 0
    assert r["resolution"].terminal_runs == 0
    assert r["resolution"].first_resolution_rate is None  # 分母 0 → None，不抛异常
    assert r["resolution"].task_success_rate is None
    assert r["recall"].online_citation_rate is None
    assert r["perf"].ttft_samples == 0 and r["perf"].ttft_p50_ms is None
    assert r["perf"].tool_success_rate is None and r["perf"].model_retry_rate is None
    assert r["dist"].total == 0 and r["dist"].interrupt_ratio is None
