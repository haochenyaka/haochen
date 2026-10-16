"""qa-auditor 质检服务：离线跑批，消费对话数据产出标签与质量信号。

不变量：
- qa-auditor 以 Durable Task 驱动（`qa.audit`），扫描增量会话，无重复标注；
- 扫描窗口与阈值由管理端 qa-auditor 配置提供，任务读取后按值构造本服务；
- 标注结果写 QALabel 表（source='auto'），可被人工复核覆盖；
- 风险告警以 admin_action_log 留痕（P7.5 日志模块）；
- 标注不阻断业务，失败不重试（下次跑批补齐）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import RATING_DOWN
from app.models.knowledge import KbDocument
from app.models.qa_label import QALabel
from app.repositories import KbEvalReportRepository

logger = logging.getLogger(__name__)

# 扫描窗口：过去 24 小时的对话（跑批间隔通常为小时级）
SCAN_WINDOW_HOURS = 24
# 过期文档阈值：超过 180 天未更新视为过期
STALE_DAYS = 180
# 低召回基线阈值
LOW_RECALL_THRESHOLD = 0.7


class QaAuditorService:
    """质检服务：自动标注扫描 + 质量信号收集。

    扫描窗口与阈值可配（管理端 qa-auditor 配置）：`qa.audit` 任务读取配置后按值构造；
    缺省取模块默认值，行为与管理端可配之前一致。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        scan_window_hours: int = SCAN_WINDOW_HOURS,
        stale_days: int = STALE_DAYS,
        low_recall_threshold: float = LOW_RECALL_THRESHOLD,
    ) -> None:
        self._session = session
        self._scan_window_hours = scan_window_hours
        self._stale_days = stale_days
        self._low_recall_threshold = low_recall_threshold

    async def auto_label_recent_threads(self) -> int:
        """扫描最近线程并自动标注，返回新标注数。

        逻辑：
        1. 获取有 downvote（点踩）的线程，标注 unresolved；
        2. 获取有转人工请求的线程，标注 handoff_requested；
        3. 获取有投诉关键字的线程，标注 complaint_risk；
        4. 合并去重，写入 QALabel。
        """
        now = datetime.now(UTC)
        window_start = now - timedelta(hours=self._scan_window_hours)
        labeled = 0

        # 1. 点踩线程 → unresolved
        unresolved_threads = await self._find_downvote_threads(window_start)
        for thread_id in unresolved_threads:
            if await self._create_auto_label(
                thread_id=thread_id,
                label="unresolved",
                confidence=0.8,
                evidence={"source": "downvote", "scan_window_hours": self._scan_window_hours},
            ):
                labeled += 1

        # 2. 转人工线程 → handoff_requested
        handoff_threads = await self._find_handoff_threads(window_start)
        for thread_id in handoff_threads:
            if await self._create_auto_label(
                thread_id=thread_id,
                label="handoff_requested",
                confidence=0.9,
                evidence={
                    "source": "handoff_request",
                    "scan_window_hours": self._scan_window_hours,
                },
            ):
                labeled += 1

        # 3. 投诉关键字 → complaint_risk
        complaint_threads = await self._find_complaint_threads(window_start)
        for thread_id in complaint_threads:
            if await self._create_auto_label(
                thread_id=thread_id,
                label="complaint_risk",
                confidence=0.7,
                evidence={"source": "keyword_match", "scan_window_hours": self._scan_window_hours},
            ):
                labeled += 1

        return labeled

    async def detect_stale_documents(self) -> list[dict[str, object]]:
        """检测过期文档：超过 STALE_DAYS 未更新的 indexed 文档。"""
        cutoff = datetime.now(UTC) - timedelta(days=self._stale_days)
        stmt = (
            select(KbDocument)
            .where(
                KbDocument.status == "indexed",
                KbDocument.updated_at < cutoff,
            )
            .limit(100)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [
            {
                "id": str(row.id),
                "title": row.title,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                "stale_days": (datetime.now(UTC) - row.updated_at).days if row.updated_at else 0,
            }
            for row in rows
        ]

    async def detect_low_recall_kbs(self) -> list[dict[str, object]]:
        """从最新 KB 评估报告中检测低召回的知识块。"""
        repo = KbEvalReportRepository(self._session)
        reports = await repo.list_recent(limit=5)
        results: list[dict[str, object]] = []
        for report in reports:
            if report.superseded_by is not None:
                continue
            metrics = report.metrics
            recall = float(metrics.get("recall_at_k", 1.0))
            if recall < self._low_recall_threshold:
                results.append(
                    {
                        "report_id": report.id,
                        "recall_at_k": recall,
                        "fingerprint": report.content_fingerprint,
                        "created_at": report.created_at.isoformat(),
                    }
                )
        return results

    async def compute_coverage_gaps(self) -> list[dict[str, object]]:
        """计算覆盖率缺口：扫描点踩消息中未覆盖的主题。

        将点踩消息的问题文本聚类，生成 FAQ 草稿建议。
        """
        now = datetime.now(UTC)
        window_start = now - timedelta(hours=self._scan_window_hours)

        # 查找点踩消息及其关联的助手消息
        stmt = text("""
            SELECT m.id, m.content
            FROM messages m
            JOIN message_feedback f ON f.message_id = m.id
            WHERE f.rating = :down
              AND f.created_at >= :window
              AND m.role = 'user'
            ORDER BY f.created_at DESC
            LIMIT 50
        """)
        rows = (
            await self._session.execute(stmt, {"down": RATING_DOWN, "window": window_start})
        ).all()

        gaps: list[dict[str, object]] = []
        for row in rows:
            content = row[1]
            if isinstance(content, dict):
                text_content = content.get("text", "")
                if isinstance(text_content, str) and text_content.strip():
                    gaps.append(
                        {
                            "message_id": str(row[0]),
                            "question": text_content[:200],
                            "status": "unaddressed",
                        }
                    )
        return gaps

    async def detect_content_conflicts(self) -> list[dict[str, object]]:
        """检测知识库内容冲突：不同文档包含相似但冲突的信息。

        当前为简化实现，按文档标题分词交叉检测重叠。
        """
        stmt = select(KbDocument).where(KbDocument.status == "indexed").limit(200)
        rows = (await self._session.execute(stmt)).scalars().all()
        # 标题相似度检测占位：按标题关键词简单分组
        conflicts: list[dict[str, object]] = []
        titles_seen: dict[str, list[str]] = {}
        for row in rows:
            # 提取标题的关键词（按空格/标点简单分割）
            import re

            keywords = set(re.findall(r"[\w\u4e00-\u9fff]+", row.title.lower()))
            for key in keywords:
                if key in titles_seen:
                    for seen_title in titles_seen[key]:
                        if seen_title != row.title:
                            conflicts.append(
                                {
                                    "document_id": str(row.id),
                                    "title": row.title,
                                    "similar_title": seen_title,
                                    "kb_id": str(row.kb_id),
                                }
                            )
                            break
            for key in keywords:
                titles_seen.setdefault(key, []).append(row.title)
        return conflicts[:50]

    async def _find_downvote_threads(self, window_start: datetime) -> set[UUID]:
        stmt = text("""
            SELECT DISTINCT m.thread_id
            FROM messages m
            JOIN message_feedback f ON f.message_id = m.id
            WHERE f.rating = :down AND f.created_at >= :window
        """)
        rows = (
            await self._session.execute(stmt, {"down": RATING_DOWN, "window": window_start})
        ).all()
        return {row[0] for row in rows}

    async def _find_handoff_threads(self, window_start: datetime) -> set[UUID]:
        stmt = text("""
            SELECT DISTINCT thread_id
            FROM handoff_requests
            WHERE created_at >= :window
        """)
        rows = (await self._session.execute(stmt, {"window": window_start})).all()
        return {row[0] for row in rows}

    async def _find_complaint_threads(self, window_start: datetime) -> set[UUID]:
        complaint_keywords = ["投诉", "举报", "差评", "不满意", "退款", "赔偿"]
        conditions = " OR ".join(
            f"m.content::text ILIKE :kw{i}" for i in range(len(complaint_keywords))
        )
        params: dict[str, Any] = {f"kw{i}": f"%{kw}%" for i, kw in enumerate(complaint_keywords)}
        params["window"] = window_start
        stmt = text(f"""
            SELECT DISTINCT m.thread_id
            FROM messages m
            WHERE m.role = 'user'
              AND m.created_at >= :window
              AND ({conditions})
            LIMIT 100
        """)
        rows = (await self._session.execute(stmt, params)).all()
        return {row[0] for row in rows}

    async def _create_auto_label(
        self,
        *,
        thread_id: UUID,
        label: str,
        confidence: float,
        evidence: dict[str, object] | None = None,
    ) -> bool:
        """创建自动标注（跳过已有同标签标注的线程）。"""
        stmt = select(QALabel).where(
            QALabel.thread_id == thread_id,
            QALabel.label == label,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return False
        auto_label = QALabel(
            thread_id=thread_id,
            label=label,
            source="auto",
            confidence=confidence,
            evidence=evidence,
            created_by="qa-auditor",
        )
        self._session.add(auto_label)
        await self._session.flush()
        return True
