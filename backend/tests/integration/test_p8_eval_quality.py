"""P8 评估闭环与数据质量集成测试（真实 PostgreSQL）。

覆盖链路：
- KB eval API 端点
- Quality scorecard 计算与落库
- QA 审计服务自动标注
- Feedback 收集与导出
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.main import create_app
from app.models.conversation import MessageFeedback
from app.models.eval import KbEvalReport
from app.models.quality_scorecard import QualityScorecard
from app.repositories.eval_repository import KbEvalReportRepository
from app.services.qa_auditor_service import QaAuditorService
from app.services.quality_service import QualityScore
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


def _add_fake_kb_report(session: AsyncSession, fp: str | None = None) -> KbEvalReport:
    """添加 KB 评估报告作为评分卡数据源。"""
    report = KbEvalReport(
        dataset_path="/fake/dataset.jsonl",
        dataset_fingerprint=fp or "test_dataset_fp",
        content_fingerprint="test_content_fp",
        config_fingerprint="test_config_fp",
        k=10,
        questions=10,
        evaluator="test",
        retrieval_config={"k": 10, "kb_codes": ["hotel_faq"]},
        metrics={
            "questions": 10,
            "k": 10,
            "recall_at_k": 0.85,
            "precision_at_k": 0.45,
            "f1_at_k": 0.59,
            "primary_top1_rate": 0.60,
            "hit_rate": 0.95,
            "answer_correctness": None,
            "answered": 0,
            "overall_score": 0.85,
            "overall_metric": "recall@10",
        },
        outcomes=[
            {
                "item_id": "test-001",
                "question": "测试问题",
                "gold_titles": ["测试文档"],
                "primary_title": "测试文档",
                "recall": 1.0,
                "precision": 0.5,
                "f1": 0.67,
                "primary_top1": True,
                "hit": True,
            }
        ],
    )
    session.add(report)
    return report


async def _add_feedback(session: AsyncSession) -> UUID:
    """添加点踩反馈并返回 thread_id。"""
    from app.models.runtime import Message, Thread, User

    # threads/messages 都要求真实的 user_id（外键）：先建一个接入身份
    user = User(username=f"p8-{uuid4().hex[:8]}", role="customer")
    session.add(user)
    await session.flush()

    thread = Thread(user_id=user.id, agent_slug="front-desk")
    session.add(thread)
    await session.flush()

    msg = Message(
        thread_id=thread.id,
        role="user",
        content={"text": "答案不对"},
        run_id=str(uuid4()),
    )
    session.add(msg)
    await session.flush()

    feedback = MessageFeedback(
        message_id=msg.id,
        user_id=user.id,
        rating="down",
    )
    session.add(feedback)
    await session.flush()
    return thread.id


def test_kb_eval_persist(runtime_db_url: str) -> None:
    """KB eval 报告落库与查询。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            _add_fake_kb_report(session)
            await session.flush()

            repo = KbEvalReportRepository(session)
            recent = await repo.list_recent(limit=5)
            return {
                "count": len(recent),
                "recall": recent[0].metrics["recall_at_k"] if recent else None,
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["count"] >= 1
    assert result["recall"] == 0.85


def test_kb_eval_invalidate(runtime_db_url: str) -> None:
    """KB eval 报告手动作废。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            report = _add_fake_kb_report(session)
            await session.flush()

            repo = KbEvalReportRepository(session)
            ok = await repo.mark_superseded(report.id, superseded_by=report.id, reason="test")
            await session.commit()

            row = await repo.get(report.id)
            return {
                "ok": ok,
                "superseded": row.superseded_by is not None,
                "reason": row.invalidated_reason,
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["ok"] is True
    assert result["superseded"] is True
    assert result["reason"] == "test"


def test_quality_scorecard(runtime_db_url: str) -> None:
    """质量评分卡计算与历史查询。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            _add_fake_kb_report(session)
            await session.flush()

        # 第一条评分卡
        async with factory() as session:
            builder = QualityScore(session)
            sc1 = await builder.compute(generated_by="test")
            await session.commit()
            id1 = sc1.id

        # 第二条评分卡
        async with factory() as session:
            builder = QualityScore(session)
            sc2 = await builder.compute(generated_by="test")
            await session.commit()
            id2 = sc2.id

        # 查询历史
        async with factory() as session:
            stmt = select(QualityScorecard).order_by(QualityScorecard.created_at.desc()).limit(10)
            rows = (await session.execute(stmt)).scalars().all()
            return {
                "id1": id1,
                "id2": id2,
                "count": len(rows),
                "scores": [r.overall_score for r in rows],
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["count"] >= 2
    # 各维度评分应在 0-1 之间
    for s in result["scores"]:
        assert 0 <= s <= 1.0


def test_qa_auditor_auto_label(runtime_db_url: str) -> None:
    """QA 自动标注：创建点踩反馈后自动打标。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            thread_id = await _add_feedback(session)
            await session.commit()

        async with factory() as session:
            qa = QaAuditorService(session)
            count = await qa.auto_label_recent_threads()
            await session.commit()

        async with factory() as session:
            from app.models.qa_label import QALabel

            stmt = select(QALabel).where(QALabel.thread_id == thread_id, QALabel.source == "auto")
            labels = (await session.execute(stmt)).scalars().all()
            return {
                "label_count": count,
                "found": len(labels),
                "labels": [label.label for label in labels],
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["label_count"] > 0
    assert result["found"] > 0
    assert "unresolved" in result["labels"]


def test_qa_auditor_detections(runtime_db_url: str) -> None:
    """QA 检测：过期文档、覆盖率缺口、低召回 KB。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            qa = QaAuditorService(session)
            stale = await qa.detect_stale_documents()
            gaps = await qa.compute_coverage_gaps()
            low = await qa.detect_low_recall_kbs()
            conflicts = await qa.detect_content_conflicts()
            return {
                "stale_type": type(stale).__name__,
                "gaps_type": type(gaps).__name__,
                "low_type": type(low).__name__,
                "conflicts_type": type(conflicts).__name__,
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["stale_type"] == "list"
    assert result["gaps_type"] == "list"
    assert result["low_type"] == "list"
    assert result["conflicts_type"] == "list"


def test_feedback_collect(runtime_db_url: str) -> None:
    """反馈收集与导出。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        # 创建反馈
        async with factory() as session:
            await _add_feedback(session)
            await session.commit()

        # 收集
        async with factory() as session:
            from app.services.feedback_service import FeedbackCollector

            collector = FeedbackCollector(session)
            since = datetime.now(UTC) - timedelta(days=1)
            down = await collector.get_downvoted_questions(since=since)
            up = await collector.get_upvoted_questions(since=since)
            ratings = await collector.get_staff_ratings(since=since)
            return {
                "down_count": len(down),
                "up_count": len(up),
                "ratings_count": len(ratings),
            }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["down_count"] >= 1
    assert isinstance(result["up_count"], int)


def test_feedback_export(runtime_db_url: str) -> None:
    """反馈导出为评估集 JSONL。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            await _add_feedback(session)
            await session.commit()

        async with factory() as session:
            from app.services.feedback_service import FeedbackCollector

            collector = FeedbackCollector(session)
            count, path = await collector.export_to_jsonl()
            return {"count": count, "path": str(path)}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["count"] >= 0
    path = Path(result["path"])
    assert path.exists()
    path.unlink(missing_ok=True)


def _test_app(factory: SessionFactoryArg) -> Any:
    """测试 app：把 get_session 覆盖到隔离库 factory（避免走默认库与跨循环连接）。"""
    from app.db import get_session

    app = create_app()
    override = get_session

    async def _override() -> Any:
        async with factory() as session:
            yield session

    app.dependency_overrides[override] = _override
    return app


def test_kb_eval_api_datasets(runtime_db_url: str) -> None:
    """KB eval API 端点：列出数据集。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        transport = ASGITransport(app=_test_app(factory))  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/admin/eval/kb/datasets", headers={"X-Dev-Username": "admin"}
            )
            return {"status": resp.status_code, "body": resp.text[:200]}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["status"] in (200, 401, 403)


def test_quality_scorecard_api(runtime_db_url: str) -> None:
    """质量评分卡 API：获取最新评分卡。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        transport = ASGITransport(app=_test_app(factory))  # type: ignore[arg-type]
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/api/admin/quality/scorecards/latest",
                headers={"X-Dev-Username": "admin"},
            )
            return {"status": resp.status_code}

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["status"] in (200, 401, 403, 404)
