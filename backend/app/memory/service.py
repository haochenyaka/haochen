"""记忆写入服务：Run 终态后提交抽取任务，校验候选后写 `guest_memories`。

提交沿用后台任务范式：调用方持有事务，与 Run 终态写入同事务提交任务意图，提交后再投递
（见 `service.py` 调用点 `RunExecutor._finalize`）。落库前一律经 `validate_candidates`
规则校验，与既有记忆（含软删）去重，保证任务重跑幂等。
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.memory.extraction import MemoryCandidate, validate_candidates
from app.models.domain import GuestMemory
from app.services.task_service import TaskService, TaskSubmission

MEMORY_EXTRACT_TASK_TYPE = "memory.extract"
MEMORY_EXTRACT_HANDLER_VERSION = 1
MEMORY_SOURCE = "auto:memory.extract"
"""自动抽取写入的记忆来源标记：与管理端手工条目区分。"""


def memory_extract_dedupe_key(run_id: UUID) -> str:
    return f"{MEMORY_EXTRACT_TASK_TYPE}:{run_id}"


async def submit_memory_extraction(
    session: AsyncSession,
    *,
    run_id: UUID,
    thread_id: UUID,
    user_id: UUID,
    agent_slug: str,
) -> TaskSubmission | None:
    """终态后提交抽取任务（同事务）；未启用抽取时返回 None，不产生任务。"""
    if not get_settings().memory_extract_enabled:
        return None
    return await TaskService(session).submit(
        task_type=MEMORY_EXTRACT_TASK_TYPE,
        handler_version=MEMORY_EXTRACT_HANDLER_VERSION,
        payload={
            "run_id": str(run_id),
            "thread_id": str(thread_id),
            "user_id": str(user_id),
            "agent_slug": agent_slug,
        },
        dedupe_key=memory_extract_dedupe_key(run_id),
    )


async def store_candidates(
    session: AsyncSession, *, guest_id: UUID, candidates: Sequence[MemoryCandidate]
) -> list[MemoryCandidate]:
    """校验后写入：与既有记忆（含软删）去重，返回实际写入的候选。"""
    if not candidates:
        return []
    existing = await _existing_keys(session, guest_id)
    kept = validate_candidates(candidates, existing=existing)
    for candidate in kept:
        session.add(
            GuestMemory(
                guest_id=guest_id,
                category=candidate.category,
                content=candidate.content,
                sensitive=False,
                source=MEMORY_SOURCE,
            )
        )
    await session.flush()
    return kept


async def _existing_keys(session: AsyncSession, guest_id: UUID) -> set[tuple[str, str]]:
    stmt = select(GuestMemory.category, GuestMemory.content).where(GuestMemory.guest_id == guest_id)
    rows = (await session.execute(stmt)).all()
    return {(row.category, row.content) for row in rows}


__all__ = [
    "MEMORY_EXTRACT_HANDLER_VERSION",
    "MEMORY_EXTRACT_TASK_TYPE",
    "MEMORY_SOURCE",
    "memory_extract_dedupe_key",
    "store_candidates",
    "submit_memory_extraction",
]
