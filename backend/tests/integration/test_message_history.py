"""长会话历史加载：保留最近消息并显式报告是否截断。"""

from __future__ import annotations

import pytest

from app.repositories import MessageRepository
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import intake_message

pytestmark = pytest.mark.integration


def test_recent_for_thread_keeps_latest_and_flags_truncation(runtime_db_url: str) -> None:
    async def _body(factory):  # type: ignore[no-untyped-def]
        async with factory() as session:
            intake = await intake_message(session, username="history-limit-tester", text="第一条")
            for index in range(4):
                await MessageRepository(session).append(
                    thread_id=intake.thread_id,
                    role="user",
                    content={"text": f"m{index}"},
                )
            await session.commit()
            recent, truncated = await MessageRepository(session).recent_for_thread(
                intake.thread_id, limit=3
            )
            complete, complete_truncated = await MessageRepository(session).recent_for_thread(
                intake.thread_id, limit=10
            )
        return {
            "recent": [row.content["text"] for row in recent],
            "truncated": truncated,
            "complete": [row.content["text"] for row in complete],
            "complete_truncated": complete_truncated,
        }

    result = run_with_session_factory(runtime_db_url, _body)
    assert result["recent"] == ["m1", "m2", "m3"]
    assert result["truncated"] is True
    assert result["complete"] == ["第一条", "m0", "m1", "m2", "m3"]
    assert result["complete_truncated"] is False
