"""PR-0.5a 自证：Steer 工位发现更新的用户消息即中止当前 Run，新请求接力。

时序：同一线程先后入队两条消息，第一条派发为 run1、第二条排队；执行 run1 时其
Steer 工位在模型调用前检测到排队的第二条 → 置取消信号并中断，run1 按取消收敛，
队头新请求经既有接力链路派发为 run2。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.agent_config import AgentConfig
from app.models.runtime import AgentRun
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.support import RecordingDispatcher, force_stub_model, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]


def test_steer_supersedes_run_when_newer_message_queued(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_stub_model(monkeypatch)

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        # 经管理端配置启用 Steer（middleware.steer.enabled=true），跑完清理（库为会话级共享）
        async with factory() as session:
            await session.execute(delete(AgentConfig).where(AgentConfig.agent_slug == "front-desk"))
            session.add(
                AgentConfig(
                    agent_slug="front-desk",
                    config_json={"middleware": {"steer": {"enabled": True}}},
                )
            )
            await session.commit()
        try:
            dispatcher = RecordingDispatcher()
            executor = RunExecutor(
                AgentRunBody(checkpointer=InMemorySaver()),
                dispatcher=dispatcher,
                session_factory=factory,
            )
            username = f"steer-{uuid4().hex[:8]}"
            async with factory() as session:
                first = await intake_message(session, username=username, text="第一问")
                assert first.run_id is not None
                # 同一线程入队第二条：run1 活跃，第二条排队、暂不派发
                second = await intake_message(
                    session, username=username, text="我改主意了", thread_id=first.thread_id
                )
                assert second.run_id is None

            # 执行 run1：其 Steer 检测到排队的第二条，取消 run1 并接力
            first_outcome = await executor.execute(first.run_id)

            async with factory() as session:
                runs = (
                    (
                        await session.execute(
                            select(AgentRun)
                            .where(AgentRun.thread_id == first.thread_id)
                            .order_by(AgentRun.created_at)
                        )
                    )
                    .scalars()
                    .all()
                )
            return {
                "first_outcome": first_outcome,
                "run_statuses": [(str(run.id), run.status) for run in runs],
                "first_run_id": str(first.run_id),
                "enqueued": [str(rid) for rid in dispatcher.enqueued],
            }
        finally:
            async with factory() as session:
                await session.execute(
                    delete(AgentConfig).where(AgentConfig.agent_slug == "front-desk")
                )
                await session.commit()

    result = run_with_session_factory(runtime_db_url, _body)
    # run1 被更新消息接管，按取消收敛（非 failed）
    assert result["first_outcome"] == "cancelled"
    statuses = dict(result["run_statuses"])
    assert statuses[result["first_run_id"]] == "cancelled"
    # 接力：为第二条消息创建了新 Run 并派发
    assert len(result["run_statuses"]) >= 2
    dispatched = set(result["enqueued"])
    second_run_ids = {rid for rid, _ in result["run_statuses"]} - {result["first_run_id"]}
    assert second_run_ids & dispatched
