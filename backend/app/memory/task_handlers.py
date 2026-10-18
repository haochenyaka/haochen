"""记忆抽取任务处理器：Run 终态后从对话抽取候选记忆 → 校验 → 写 `guest_memories`。

触发点：Run 以 completed 收敛时（`RunExecutor._finalize`）提交 `memory.extract` 任务。
处理器复核 Run 确为 completed 才抽取——任务可能因恢复补发而在 Run 非终态时被执行，复核即
失败关闭的前置守卫。无客人档案、空对话、未启用抽取都按"无害跳过"返回，不置任务失败。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.memory.extraction import build_memory_extractor
from app.memory.service import (
    MEMORY_EXTRACT_HANDLER_VERSION,
    MEMORY_EXTRACT_TASK_TYPE,
    store_candidates,
)
from app.models.runtime import Message
from app.repositories import AgentRunRepository, GuestRepository, MessageRepository
from app.services.task_registry import TaskContext, task_handler

logger = logging.getLogger(__name__)

TRANSCRIPT_MESSAGE_LIMIT = 20
TRANSCRIPT_MAX_CHARS = 4000
ROLE_LABELS = {"user": "客人", "assistant": "助手"}


class MemoryTaskError(Exception):
    """载荷非法：无法定位 Run 与客人，任务据此失败关闭。"""


def _payload_uuid(ctx: TaskContext, key: str) -> UUID | None:
    raw = ctx.payload_value(key)
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def _message_text(content: Any) -> str:
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def render_transcript(rows: Sequence[Message]) -> str:
    """对话转写：取末尾若干条客人/助手消息，拼成"角色：文本"的多行文本并截断。"""
    lines: list[str] = []
    for row in rows[-TRANSCRIPT_MESSAGE_LIMIT:]:
        label = ROLE_LABELS.get(row.role)
        if label is None:
            continue
        text = _message_text(row.content).strip()
        if text:
            lines.append(f"{label}：{text}")
    transcript = "\n".join(lines)
    return transcript[-TRANSCRIPT_MAX_CHARS:]


@task_handler(MEMORY_EXTRACT_TASK_TYPE, MEMORY_EXTRACT_HANDLER_VERSION)
async def extract_memories(ctx: TaskContext) -> Mapping[str, Any]:
    settings = get_settings()
    if not settings.memory_extract_enabled:
        return {"skipped": "disabled"}

    run_id = _payload_uuid(ctx, "run_id")
    thread_id = _payload_uuid(ctx, "thread_id")
    user_id = _payload_uuid(ctx, "user_id")
    if run_id is None or thread_id is None or user_id is None:
        raise MemoryTaskError("memory.extract 缺少 run_id/thread_id/user_id 载荷")

    async with ctx.session_factory() as session:
        run = await AgentRunRepository(session).get_by_id(run_id)
        if run is None or run.status != "completed":
            return {"skipped": "run_not_completed"}
        guest = await GuestRepository(session).get_by_user_id(user_id)
        if guest is None:
            return {"skipped": "no_guest"}
        guest_id = guest.id
        history = await MessageRepository(session).latest_for_thread(
            thread_id, limit=TRANSCRIPT_MESSAGE_LIMIT
        )
        transcript = render_transcript(history)

    if not transcript.strip():
        return {"stored": 0, "reason": "empty_transcript"}

    candidates = await build_memory_extractor().extract(transcript=transcript)
    async with ctx.session_factory() as session:
        kept = await store_candidates(session, guest_id=guest_id, candidates=candidates)
        await session.commit()

    logger.info(
        "记忆抽取完成：run_id=%s candidates=%d stored=%d",
        run_id,
        len(candidates),
        len(kept),
    )
    return {"candidates": len(candidates), "stored": len(kept)}


__all__ = ["MemoryTaskError", "extract_memories", "render_transcript"]
