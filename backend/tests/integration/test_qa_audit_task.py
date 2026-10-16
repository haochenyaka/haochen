"""qa.audit 跑批任务集成测试（真实 PostgreSQL）：提交 → 执行 → 标注落库与去重。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.conversation import MessageFeedback
from app.models.qa_label import QALabel
from app.models.runtime import Message, Thread, User
from app.quality.task_handlers import submit_qa_audit
from app.repositories import TaskRepository
from app.services.task_service import TaskExecutor
from tests.integration.db_utils import run_with_session_factory

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


async def _seed_downvoted_thread(session: AsyncSession) -> UUID:
    """建一个含点踩反馈的线程，供自动标注命中 unresolved。"""
    user = User(username=f"qa-{uuid4().hex[:8]}", role="customer")
    session.add(user)
    await session.flush()

    thread = Thread(user_id=user.id, agent_slug="front-desk")
    session.add(thread)
    await session.flush()

    message = Message(
        thread_id=thread.id,
        role="user",
        content={"text": "答案不对"},
        run_id=str(uuid4()),
    )
    session.add(message)
    await session.flush()

    session.add(MessageFeedback(message_id=message.id, user_id=user.id, rating="down"))
    await session.flush()
    return thread.id


def test_qa_audit_task_labels_threads(runtime_db_url: str) -> None:
    """提交 qa.audit 并经执行器跑一轮：点踩线程被自动标注为 unresolved。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        async with factory() as session:
            thread_id = await _seed_downvoted_thread(session)
            await session.commit()

        submission = await submit_qa_audit(
            dedupe_key=f"qa.audit:test-{uuid4().hex[:8]}",
            session_factory=factory,
        )
        outcome = await TaskExecutor(session_factory=factory).execute(submission.task_id)

        async with factory() as session:
            task = await TaskRepository(session).get_by_id(submission.task_id)
            assert task is not None
            labels = (
                (
                    await session.execute(
                        select(QALabel).where(
                            QALabel.thread_id == thread_id, QALabel.source == "auto"
                        )
                    )
                )
                .scalars()
                .all()
            )
            return {
                "outcome": outcome,
                "status": task.status,
                "result": task.result,
                "labels": [label.label for label in labels],
            }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "succeeded"
    assert values["status"] == "succeeded"
    assert values["result"]["labels_created"] >= 1
    assert "unresolved" in values["labels"]


def test_qa_audit_submit_deduplicates_within_hour(runtime_db_url: str) -> None:
    """同一小时去重键：重复提交命中同一任务，不产生第二个 run。"""

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        key = f"qa.audit:test-{uuid4().hex[:8]}"
        first = await submit_qa_audit(dedupe_key=key, session_factory=factory)
        second = await submit_qa_audit(dedupe_key=key, session_factory=factory)
        return {
            "same_id": first.task_id == second.task_id,
            "first_deduplicated": first.deduplicated,
            "second_deduplicated": second.deduplicated,
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["same_id"] is True
    assert values["first_deduplicated"] is False
    assert values["second_deduplicated"] is True
