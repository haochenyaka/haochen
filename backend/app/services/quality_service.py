"""数据质量评分卡：从评估报告、质检标注与审计事实面派生五个维度评分。

不变量：
- 每次计算生成新快照行（QualityScorecard），不改写历史；
- 评分卡数据完全从已有审计/评估/质检事实派生，不产生第二份统计副本；
- 五个维度独立算分后加权聚合为总分。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.qa_label import QALabel
from app.models.quality_scorecard import QualityScorecard
from app.repositories.eval_agent_repo import AgentEvalReportRepository
from app.repositories.eval_repository import KbEvalReportRepository
from app.services.qa_auditor_service import QaAuditorService

logger = logging.getLogger(__name__)

# 各维度权重
WEIGHT_COVERAGE = 0.25
WEIGHT_FRESHNESS = 0.15
WEIGHT_CORRECTNESS = 0.25
WEIGHT_CONSISTENCY = 0.15
WEIGHT_RECALL = 0.20


class QualityScore:
    """评分卡构建器：收集各维度数据并计算分数。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._qa = QaAuditorService(session)

    async def compute(self, generated_by: str = "manual") -> QualityScorecard:
        """执行完整评分卡计算并落库。"""
        # 1. 覆盖率
        coverage_score, gaps, actionable_c = await self._score_coverage()
        coverage_detail = {
            "total_gaps": gaps,
            "actionable": actionable_c,
            "sample_gaps": (await self._qa.compute_coverage_gaps())[:10],
        }

        # 2. 新鲜度
        freshness_score, expired, actionable_f = await self._score_freshness()
        fresh_detail = {"expired_documents": expired, "actionable": actionable_f}

        # 3. 正确性
        correctness_score, issues, actionable_corr = await self._score_correctness()
        correctness_detail = {
            "total_issues": issues,
            "actionable": actionable_corr,
            "hallucination_labels": issues,
        }

        # 4. 一致性
        consistency_score, conflicts, actionable_cons = await self._score_consistency()
        consistency_detail = {
            "conflicts": conflicts,
            "actionable": actionable_cons,
        }

        # 5. 召回质量
        recall_score, low_count, actionable_r = await self._score_recall_quality()
        recall_detail = {
            "low_recall_count": low_count,
            "actionable": actionable_r,
        }

        # 综合评分
        overall = (
            coverage_score * WEIGHT_COVERAGE
            + freshness_score * WEIGHT_FRESHNESS
            + correctness_score * WEIGHT_CORRECTNESS
            + consistency_score * WEIGHT_CONSISTENCY
            + recall_score * WEIGHT_RECALL
        )

        # 获取最新评估报告引用
        kb_report_id = await self._latest_kb_eval_id()
        agent_report_id = await self._latest_agent_eval_id()

        summary = self._build_summary(
            coverage_score,
            freshness_score,
            correctness_score,
            consistency_score,
            recall_score,
            overall,
            gaps,
            expired,
            issues,
            conflicts,
            low_count,
        )

        scorecard = QualityScorecard(
            generated_by=generated_by,
            coverage_score=coverage_score,
            coverage_gaps=gaps,
            coverage_actionable=actionable_c,
            coverage_detail=coverage_detail,
            freshness_score=freshness_score,
            freshness_expired=expired,
            freshness_actionable=actionable_f,
            freshness_detail=fresh_detail,
            correctness_score=correctness_score,
            correctness_issues=issues,
            correctness_actionable=actionable_corr,
            correctness_detail=correctness_detail,
            consistency_score=consistency_score,
            consistency_conflicts=conflicts,
            consistency_actionable=actionable_cons,
            consistency_detail=consistency_detail,
            recall_quality_score=recall_score,
            recall_low_count=low_count,
            recall_actionable=actionable_r,
            recall_quality_detail=recall_detail,
            overall_score=round(overall, 4),
            summary=summary,
            latest_kb_eval_report_id=kb_report_id,
            latest_agent_eval_report_id=agent_report_id,
        )
        self._session.add(scorecard)
        await self._session.flush()
        return scorecard

    async def _score_coverage(self) -> tuple[float, int, int]:
        """覆盖率评分：基于知识缺口数。

        理想缺口数为 0，每发现一个缺口约降 0.05 分，最低 0.3。
        """
        gaps = await self._qa.compute_coverage_gaps()
        gap_count = len(gaps)
        score = max(0.3, 1.0 - gap_count * 0.05)
        return round(score, 4), gap_count, max(0, gap_count)

    async def _score_freshness(self) -> tuple[float, int, int]:
        """新鲜度评分：检测过期文档。

        每篇过期文档约降 0.02 分，最低 0.3。
        """
        stale = await self._qa.detect_stale_documents()
        stale_count = len(stale)
        score = max(0.3, 1.0 - stale_count * 0.02)
        return round(score, 4), stale_count, stale_count

    async def _score_correctness(self) -> tuple[float, int, int]:
        """正确性评分：基于质检标注中的幻觉嫌疑数。

        每个幻觉嫌疑约降 0.1 分，最低 0.2。
        """
        stmt = select(func.count(QALabel.id)).where(
            QALabel.label == "hallucination_suspected",
            QALabel.created_at >= datetime.now(UTC) - timedelta(days=7),
        )
        count = (await self._session.execute(stmt)).scalar() or 0
        score = max(0.2, 1.0 - count * 0.1)
        return round(score, 4), int(count), int(count)

    async def _score_consistency(self) -> tuple[float, int, int]:
        """一致性评分：检测内容冲突。

        每个冲突约降 0.05 分，最低 0.3。
        """
        conflicts = await self._qa.detect_content_conflicts()
        conflict_count = len(conflicts)
        score = max(0.3, 1.0 - conflict_count * 0.05)
        return round(score, 4), conflict_count, conflict_count

    async def _score_recall_quality(self) -> tuple[float, int, int]:
        """召回质量评分：基于最新 KB 评估报告的 recall@K。

        低于 0.7 阈值每低 0.1 约降 0.1 分，最低 0.2。
        无可比报告时取默认 0.8。
        """
        repo = KbEvalReportRepository(self._session)
        reports = await repo.list_recent(limit=5)
        low_count = 0
        best_recall = 0.8
        for report in reports:
            if report.superseded_by is not None:
                continue
            recall = float(report.metrics.get("recall_at_k", 0.8))
            best_recall = max(best_recall, recall)
            if recall < 0.7:
                low_count += 1
        # 将 recall 映射为评分：recall=1.0 → 1.0, recall=0.5 → 0.5
        score = max(0.2, best_recall)
        return round(score, 4), low_count, low_count

    async def _latest_kb_eval_id(self) -> int | None:
        repo = KbEvalReportRepository(self._session)
        reports = await repo.list_recent(limit=1)
        return reports[0].id if reports else None

    async def _latest_agent_eval_id(self) -> int | None:
        repo = AgentEvalReportRepository(self._session)
        reports = await repo.list_recent(limit=1)
        return reports[0].id if reports else None

    def _build_summary(
        self,
        coverage: float,
        freshness: float,
        correctness: float,
        consistency: float,
        recall: float,
        overall: float,
        gaps: int,
        expired: int,
        issues: int,
        conflicts: int,
        low_recall: int,
    ) -> str:
        parts: list[str] = [
            f"综合评分 {overall:.1%}",
        ]
        if gaps:
            parts.append(f"知识缺口 {gaps} 个")
        if expired:
            parts.append(f"过期文档 {expired} 篇")
        if issues:
            parts.append(f"幻觉嫌疑 {issues} 条")
        if conflicts:
            parts.append(f"内容冲突 {conflicts} 处")
        if low_recall:
            parts.append(f"低召回 {low_recall} 项")
        return "；".join(parts) if len(parts) > 1 else parts[0]
