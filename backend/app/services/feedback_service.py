"""在线反馈数据管道：收集 C 端点赞/点踩与保洁评分，汇聚后供评估集扩充与 Langfuse score。

不变量：
- 反馈以 PostgreSQL 业务表（message_feedback / attendance_checkins）为权威事实；
- Langfuse 反馈 score 是可选副本，写入失败不阻断业务流程；
- 评估集导出路径输出为 JSONL，与 kb_faq.jsonl 同格式，可追加到出厂评估集。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.langfuse_service import get_langfuse

logger = logging.getLogger(__name__)

FEEDBACK_WINDOW_DAYS = 7

# 评估集导出目录（默认：app/eval/imported/）
IMPORTED_DIR = Path(__file__).parent.parent / "eval" / "imported"


class FeedbackCollector:
    """反馈收集器：聚合各类反馈数据并提供导出管道。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_downvoted_questions(
        self, since: datetime | None = None
    ) -> list[dict[str, object]]:
        """获取点踩消息及其关联的助手回复，用于评估集补充。"""
        since = since or datetime.now(UTC) - timedelta(days=FEEDBACK_WINDOW_DAYS)
        stmt = text("""
            SELECT
                f.id AS feedback_id,
                user_msg.id AS user_message_id,
                user_msg.content AS user_content,
                assistant_msg.id AS assistant_message_id,
                assistant_msg.content AS assistant_content,
                f.created_at
            FROM message_feedback f
            JOIN messages user_msg ON user_msg.id = f.message_id
            LEFT JOIN LATERAL (
                SELECT m.id, m.content
                FROM messages m
                WHERE m.thread_id = user_msg.thread_id
                  AND m.role = 'assistant'
                  AND m.created_at > user_msg.created_at
                ORDER BY m.created_at
                LIMIT 1
            ) assistant_msg ON true
            WHERE f.rating = :down
              AND f.created_at >= :since
              AND user_msg.role = 'user'
            ORDER BY f.created_at DESC
            LIMIT 200
        """)
        rows = (await self._session.execute(stmt, {"down": "down", "since": since})).all()
        results: list[dict[str, object]] = []
        for row in rows:
            user_text = ""
            if isinstance(row.user_content, dict):
                user_text = row.user_content.get("text", "") or ""
            assistant_text = ""
            if isinstance(row.assistant_content, dict):
                assistant_text = row.assistant_content.get("text", "") or ""
            results.append({
                "feedback_id": str(row.feedback_id),
                "question": user_text[:500],
                "assistant_answer": assistant_text[:1000],
                "rating": "down",
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })
        return results

    async def get_upvoted_questions(self, since: datetime | None = None) -> list[dict[str, object]]:
        """获取点赞消息作为正面示例。"""
        since = since or datetime.now(UTC) - timedelta(days=FEEDBACK_WINDOW_DAYS)
        stmt = text("""
            SELECT
                f.id AS feedback_id,
                user_msg.id AS user_message_id,
                user_msg.content AS user_content,
                f.created_at
            FROM message_feedback f
            JOIN messages user_msg ON user_msg.id = f.message_id
            WHERE f.rating = :up
              AND f.created_at >= :since
              AND user_msg.role = 'user'
            ORDER BY f.created_at DESC
            LIMIT 200
        """)
        rows = (await self._session.execute(stmt, {"up": "up", "since": since})).all()
        results: list[dict[str, object]] = []
        for row in rows:
            user_text = ""
            if isinstance(row.user_content, dict):
                user_text = row.user_content.get("text", "") or ""
            results.append({
                "feedback_id": str(row.feedback_id),
                "question": user_text[:500],
                "rating": "up",
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })
        return results

    async def get_staff_ratings(self, since: datetime | None = None) -> list[dict[str, object]]:
        """获取保洁评分作为服务质量反馈。"""
        since = since or datetime.now(UTC) - timedelta(days=FEEDBACK_WINDOW_DAYS)
        stmt = text("""
            SELECT
                hk.id AS task_id,
                hk.room_id,
                hk.task_type,
                hk.guest_score,
                hk.guest_comment,
                hk.verified_at
            FROM housekeeping_tasks hk
            WHERE hk.guest_score IS NOT NULL
              AND hk.verified_at >= :since
            ORDER BY hk.verified_at DESC
            LIMIT 100
        """)
        rows = (await self._session.execute(stmt, {"since": since})).all()
        return [
            {
                "task_id": str(row.task_id),
                "room_id": str(row.room_id) if row.room_id else None,
                "task_type": row.task_type,
                "score": float(row.guest_score) if row.guest_score else None,
                "comment": row.guest_comment,
                "verified_at": row.verified_at.isoformat() if row.verified_at else None,
            }
            for row in rows
        ]

    async def export_to_jsonl(self, output_path: str | Path | None = None) -> Path:
        """将在线反馈导出为 JSONL 格式评估集。

        输出文件可用于追加到 kb_faq.jsonl 或作为独立 feedback 评估集。
        """
        path = Path(output_path) if output_path else IMPORTED_DIR / "feedback_import.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)

        downvoted = await self.get_downvoted_questions()
        items: list[dict[str, object]] = []
        for item in downvoted:
            items.append({
                "id": f"feedback-{item['feedback_id']}",
                "question": item["question"],
                "kb_code": "hotel_faq",
                "gold_titles": ["[feedback_negative]"],
                "primary_title": "[feedback_negative]",
                "gold_answer": item.get("assistant_answer", ""),
                "tags": ["feedback", "negative"],
            })

        with open(path, "w", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")

        logger.info("反馈评估集已导出：%s（%d 条）", path, len(items))
        return len(items), path

    async def sync_feedback_to_langfuse(self) -> int:
        """将本地反馈同步到 Langfuse score（可选，有 trace 才写）。"""
        lf = get_langfuse()
        if not lf.enabled:
            return 0

        since = datetime.now(UTC) - timedelta(hours=1)
        stmt = text("""
            SELECT f.rating, r.langfuse_trace_id, f.id
            FROM message_feedback f
            JOIN messages m ON m.id = f.message_id
            JOIN agent_runs r ON r.id = m.run_id::uuid
            WHERE f.created_at >= :since
              AND r.langfuse_trace_id IS NOT NULL
            LIMIT 100
        """)
        rows = (await self._session.execute(stmt, {"since": since})).all()
        count = 0
        for row in rows:
            value = 1.0 if row.rating == "up" else 0.0
            lf.score_feedback(
                trace_id=row.langfuse_trace_id,
                name="user_feedback",
                value=value,
            )
            count += 1
        lf.flush()
        return count