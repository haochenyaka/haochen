"""知识库工具集成测试（真实 PostgreSQL）：执行处复验、引用回灌与引用清单落审计。

外部依赖（MinIO / Milvus / 嵌入接口）走进程内替身；检索事实以 PG 的 kb_chunks 为准，
因此断言可以逐字比对正文与引用字段。最后一个用例走完整 Run 链路（图 → 工具 → 审计），
验证"检索 → 引用 → model_audit.retrieval_refs"闭环。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents import StaticResourcePolicy
from app.agents.buildin.front_desk.agent import FrontDeskAgent
from app.agents.context import ResourceRef, ResourceSnapshot
from app.agents.runtime import RuntimeContext
from app.agents.toolkits import default_registry
from app.config import get_settings
from app.knowledge import retrieval as retrieval_module
from app.knowledge import task_handlers
from app.knowledge.embedding import DeterministicEmbeddings
from app.knowledge.upload_service import KnowledgeUploadService
from app.models.audit import ModelAudit, ToolAudit
from app.repositories import KbDocumentRepository, KnowledgeBaseRepository
from app.services.audit_service import AuditRecorder
from app.services.run_body import AgentRunBody
from app.services.run_worker import RunExecutor
from app.services.task_service import TaskExecutor
from tests.integration.db_utils import run_with_session_factory
from tests.integration.knowledge_fakes import (
    FakeObjectStorage,
    FakeVectorStore,
    RecordingTaskDispatcher,
)
from tests.integration.support import RecordingDispatcher, ScriptedToolChatModel, intake_message

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

BREAKFAST_TEXT = (
    "# 早餐说明\n\n"
    "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅，凭房号入座，无需餐券。\n\n"
    "房价不含早餐，单独购买 38 元/位，1.2 米以下儿童 19 元/位。"
)


@pytest.fixture(autouse=True)
def _clean_fakes() -> None:
    FakeObjectStorage.reset()
    FakeVectorStore.reset()
    yield
    FakeObjectStorage.reset()
    FakeVectorStore.reset()


@pytest.fixture(autouse=True)
def _patch_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_handlers, "ObjectStorage", FakeObjectStorage)
    monkeypatch.setattr(task_handlers, "ChunkVectorStore", FakeVectorStore)
    monkeypatch.setattr(task_handlers, "build_embedding_provider", DeterministicEmbeddings)
    monkeypatch.setattr(retrieval_module, "ChunkVectorStore", FakeVectorStore)
    monkeypatch.setattr(retrieval_module, "build_embedding_provider", DeterministicEmbeddings)
    monkeypatch.setattr(get_settings(), "kb_chunk_max_chars", 120)


async def _call(tool_name: str, context: RuntimeContext | None, **kwargs: Any) -> str:
    tool = default_registry().get(tool_name)
    runtime = SimpleNamespace(context=context)
    return await tool.coroutine(runtime=runtime, **kwargs)


async def _create_knowledge_base(
    factory: SessionFactoryArg, *, code: str, scope: str = "guest", active: bool = True
) -> Any:
    async with factory() as session:
        kb = await KnowledgeBaseRepository(session).create(
            code=code, name=f"测试库 {code}", scope=scope
        )
        if not active:
            kb.active = False
        await session.commit()
        return kb.id


async def _index_document(
    factory: SessionFactoryArg, *, kb_id: Any, title: str, data: bytes
) -> Any:
    async with factory() as session:
        outcome = await KnowledgeUploadService(session, storage=FakeObjectStorage()).upload(
            kb_id=kb_id,
            title=title,
            source_type="markdown",
            filename=f"{title}.md",
            data=data,
        )
        document_id = outcome.document.id
        parse_task_id = outcome.submission.task_id

    dispatcher = RecordingTaskDispatcher()
    executor = TaskExecutor(dispatcher=dispatcher, session_factory=factory)
    assert await executor.execute(parse_task_id) == "succeeded"
    assert await executor.execute(dispatcher.enqueued[-1]) == "succeeded"
    async with factory() as session:
        document = await KbDocumentRepository(session).get_by_id(document_id)
        assert document is not None
        assert document.status == "indexed"
    return document_id


def _runtime_context(
    factory: SessionFactoryArg, *codes: str, agent_slug: str = "front-desk"
) -> RuntimeContext:
    return RuntimeContext(
        agent_slug=agent_slug,
        user_id=uuid4(),
        thread_id=uuid4(),
        run_id=uuid4(),
        actor_id="tool-test",
        resources=ResourceSnapshot(
            knowledge_bases=tuple(ResourceRef("knowledge_base", code) for code in codes)
        ),
        config={},
        prompt_docs="",
        checkpointer=None,
        recorder=AuditRecorder(run_id=uuid4(), agent_slug=agent_slug, session_factory=factory),
        session_factory=factory,
    )


def test_query_kb_returns_cited_context_and_collects_refs(runtime_db_url: str) -> None:
    code = f"tools-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        kb_id = await _create_knowledge_base(factory, code=code)
        await _index_document(factory, kb_id=kb_id, title="早餐说明", data=BREAKFAST_TEXT.encode())
        context = _runtime_context(factory, code)
        result = await _call("query_kb", context, question="早餐时间是几点")
        refs = context.retrieval_refs.drain()
        return {"result": result, "refs": refs, "second_drain": context.retrieval_refs.drain()}

    values = run_with_session_factory(runtime_db_url, _body)
    assert "7:00-9:30" in values["result"]
    assert "[1]《早餐说明》第 " in values["result"]
    assert "标注编号" in values["result"]
    refs = values["refs"]
    assert refs and refs[0]["document_title"] == "早餐说明"
    assert refs[0]["chunk_id"] and refs[0]["document_id"]
    assert isinstance(refs[0]["index"], int) and refs[0]["index"] == 1
    assert any("7:00-9:30" in str(ref["snippet"]) for ref in refs)
    # 取走即清空：下一轮模型调用不会重复计入上一轮引用
    assert values["second_drain"] == []


def test_query_kb_fails_closed_when_database_disagrees_with_snapshot(runtime_db_url: str) -> None:
    active_code = f"active-{uuid4().hex[:8]}"
    inactive_code = f"inactive-{uuid4().hex[:8]}"
    staff_code = f"staff-{uuid4().hex[:8]}"
    missing_code = f"missing-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        await _create_knowledge_base(factory, code=active_code)
        await _create_knowledge_base(factory, code=inactive_code, active=False)
        await _create_knowledge_base(factory, code=staff_code, scope="staff")
        context = _runtime_context(factory, active_code, inactive_code, staff_code, missing_code)
        return {
            "inactive": await _call(
                "query_kb", context, question="早餐几点", kb_ids=[inactive_code]
            ),
            "staff": await _call("query_kb", context, question="早餐几点", kb_ids=[staff_code]),
            "missing": await _call("query_kb", context, question="早餐几点", kb_ids=[missing_code]),
            "mixed": await _call(
                "query_kb", context, question="早餐几点", kb_ids=[active_code, staff_code]
            ),
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert "知识库已停用" in values["inactive"]
    assert "不在可检索范围" in values["staff"]
    assert "知识库不存在" in values["missing"]
    assert "不在可检索范围" in values["mixed"]


def test_list_kbs_filters_to_active_guest_scope(runtime_db_url: str) -> None:
    active_code = f"list-{uuid4().hex[:8]}"
    inactive_code = f"list-off-{uuid4().hex[:8]}"
    staff_code = f"list-staff-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, str]:
        await _create_knowledge_base(factory, code=active_code)
        await _create_knowledge_base(factory, code=inactive_code, active=False)
        await _create_knowledge_base(factory, code=staff_code, scope="staff")
        listed = await _call(
            "list_kbs", _runtime_context(factory, active_code, inactive_code, staff_code)
        )
        empty = await _call("list_kbs", _runtime_context(factory))
        return {"listed": listed, "empty": empty}

    values = run_with_session_factory(runtime_db_url, _body)
    assert f"- {active_code}：" in values["listed"]
    assert inactive_code not in values["listed"]
    assert staff_code not in values["listed"]
    assert values["empty"] == "检索失败：当前 Agent 没有可用的知识库"


def test_query_kb_reports_no_match_without_recording_refs(runtime_db_url: str) -> None:
    code = f"empty-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _create_knowledge_base(factory, code=code)
        context = _runtime_context(factory, code)
        result = await _call("query_kb", context, question="早餐时间是几点")
        return {"result": result, "refs": context.retrieval_refs.drain()}

    values = run_with_session_factory(runtime_db_url, _body)
    assert "没有检索到" in values["result"]
    assert values["refs"] == []


def test_run_loop_records_retrieval_refs_in_model_audit(
    runtime_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整 Run 链路：模型点名 query_kb → 工具复验检索 → 引用随模型事实落库。"""
    code = f"loop-{uuid4().hex[:8]}"
    model = ScriptedToolChatModel(tool_name="query_kb", tool_args={"question": "早餐时间是几点"})
    monkeypatch.setattr(
        "app.agents.buildin.front_desk.agent.build_chat_model", lambda **kwargs: model
    )
    monkeypatch.setattr(FrontDeskAgent, "knowledge_base_ids", (code,))

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        from langgraph.checkpoint.memory import InMemorySaver

        kb_id = await _create_knowledge_base(factory, code=code)
        document_id = await _index_document(
            factory, kb_id=kb_id, title="早餐说明", data=BREAKFAST_TEXT.encode()
        )
        executor = RunExecutor(
            AgentRunBody(
                checkpointer=InMemorySaver(),
                resource_policy=StaticResourcePolicy(
                    allowed=frozenset({("tool", "query_kb"), ("knowledge_base", code)})
                ),
            ),
            dispatcher=RecordingDispatcher(),
            session_factory=factory,
        )
        async with factory() as session:
            intake = await intake_message(session, username="kb-tool-tester", text="早餐几点")
        assert intake.run_id is not None
        outcome = await executor.execute(intake.run_id)

        async with factory() as session:
            tool_rows = (
                (
                    await session.execute(
                        select(ToolAudit)
                        .where(ToolAudit.run_id == str(intake.run_id))
                        .order_by(ToolAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
            model_rows = (
                (
                    await session.execute(
                        select(ModelAudit)
                        .where(ModelAudit.run_id == str(intake.run_id))
                        .order_by(ModelAudit.seq)
                    )
                )
                .scalars()
                .all()
            )
        return {
            "outcome": outcome,
            "document_id": str(document_id),
            "seen_tools": model.seen_tools,
            "tool_rows": [(row.tool_name, row.status, row.args_digest) for row in tool_rows],
            "refs": model_rows[-1].retrieval_refs,
            "refs_by_seq": [(row.seq, row.retrieval_refs) for row in model_rows],
        }

    values = run_with_session_factory(runtime_db_url, _body)
    assert values["outcome"] == "completed"
    # 知识库资源与 query_kb 授权后，其余工具被归一化剔除：模型只见 query_kb
    assert values["seen_tools"] == [["query_kb"], ["query_kb"]]
    assert values["tool_rows"] == [("query_kb", "completed", {"question": "早餐时间是几点"})]
    # 首次模型调用（发出工具调用）无引用；带工具结果的答复调用记录引用清单
    assert values["refs_by_seq"][0][1] is None
    refs = values["refs"]
    assert refs and refs[0]["document_id"] == values["document_id"]
    assert refs[0]["document_title"] == "早餐说明"
    assert any("7:00-9:30" in str(ref["snippet"]) for ref in refs)
