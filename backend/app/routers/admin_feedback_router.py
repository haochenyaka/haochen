"""在线反馈接口：查看/导出反馈数据，同步 Langfuse score。

权限：admin/manager。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.security import Identity, require_roles
from app.services.feedback_service import FeedbackCollector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/feedback", tags=["admin-feedback"])

feedback_guard = require_roles("admin", "manager")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
GuardDep = Annotated[Identity, Depends(feedback_guard)]


class FeedbackItem(BaseModel):
    feedback_id: str
    question: str
    assistant_answer: str | None = None
    rating: str
    created_at: str | None = None


class StaffRatingItem(BaseModel):
    task_id: str
    room_id: str | None = None
    task_type: str | None = None
    score: float | None = None
    comment: str | None = None
    verified_at: str | None = None


class ExportResult(BaseModel):
    path: str
    count: int


class SyncResult(BaseModel):
    langfuse_synced: int


@router.get("/downvoted", response_model=list[FeedbackItem])
async def list_downvoted(
    session: SessionDep,
    _identity: GuardDep,
    days: int = 7,
) -> list[FeedbackItem]:
    """列出点踩反馈。"""
    since = datetime.now(UTC) - timedelta(days=days)
    collector = FeedbackCollector(session)
    items = await collector.get_downvoted_questions(since=since)
    return [
        FeedbackItem(
            feedback_id=str(item["feedback_id"]),
            question=str(item.get("question", "")),
            assistant_answer=(
                str(item.get("assistant_answer")) if item.get("assistant_answer") else None
            ),
            rating="down",
            created_at=str(item.get("created_at")),
        )
        for item in items
    ]


@router.get("/upvoted", response_model=list[FeedbackItem])
async def list_upvoted(
    session: SessionDep,
    _identity: GuardDep,
    days: int = 7,
) -> list[FeedbackItem]:
    """列出点赞反馈。"""
    since = datetime.now(UTC) - timedelta(days=days)
    collector = FeedbackCollector(session)
    items = await collector.get_upvoted_questions(since=since)
    return [
        FeedbackItem(
            feedback_id=str(item["feedback_id"]),
            question=str(item.get("question", "")),
            rating="up",
            created_at=str(item.get("created_at")),
        )
        for item in items
    ]


@router.get("/staff-ratings", response_model=list[StaffRatingItem])
async def list_staff_ratings(
    session: SessionDep,
    _identity: GuardDep,
    days: int = 7,
) -> list[StaffRatingItem]:
    """列出保洁评分。"""
    since = datetime.now(UTC) - timedelta(days=days)
    collector = FeedbackCollector(session)
    items = await collector.get_staff_ratings(since=since)
    return [StaffRatingItem(**item) for item in items]


@router.post("/export", response_model=ExportResult)
async def export_feedback(
    session: SessionDep,
    _identity: GuardDep,
) -> ExportResult:
    """导出反馈为评估集 JSONL。"""
    collector = FeedbackCollector(session)
    count, path = await collector.export_to_jsonl()
    return ExportResult(path=str(path), count=count)


@router.post("/sync-langfuse", response_model=SyncResult)
async def sync_to_langfuse(
    session: SessionDep,
    _identity: GuardDep,
) -> SyncResult:
    """同步反馈到 Langfuse score。"""
    collector = FeedbackCollector(session)
    count = await collector.sync_feedback_to_langfuse()
    return SyncResult(langfuse_synced=count)