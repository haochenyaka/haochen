"""知识库工具单元验证：注册元数据、运行期上下文门禁与引用收集器语义。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.agents.buildin.front_desk.agent import HOTEL_KNOWLEDGE_BASE, FrontDeskAgent
from app.agents.context import ResourceRef, ResourceSnapshot
from app.agents.runtime import RetrievalRefCollector, RuntimeContext
from app.agents.toolkits import default_registry
from app.knowledge.seed import KB_CODE
from app.services.audit_service import AuditRecorder


def test_knowledge_tools_are_registered_as_read_only_knowledge_category() -> None:
    registry = default_registry()
    assert {"query_kb", "list_kbs"} <= set(registry.names())
    for name in ("query_kb", "list_kbs"):
        tool = registry.get(name)
        metadata = tool.metadata or {}
        assert metadata["category"] == "knowledge"
        assert metadata["read_only"] is True
        assert name in registry.read_only_names()


def test_query_kb_schema_hides_runtime_parameter() -> None:
    tool = default_registry().get("query_kb")
    assert set(tool.args) == {"question", "kb_ids", "top_n"}
    assert "runtime" not in str(tool.tool_call_schema.model_json_schema())


def test_front_desk_declares_the_seeded_guest_knowledge_base() -> None:
    assert HOTEL_KNOWLEDGE_BASE == KB_CODE
    assert FrontDeskAgent.knowledge_base_ids == (KB_CODE,)
    assert {"query_kb", "list_kbs"} <= set(FrontDeskAgent.tool_ids)


def _context(*codes: str, agent_slug: str = "front-desk") -> RuntimeContext:
    return RuntimeContext(
        agent_slug=agent_slug,
        user_id=uuid4(),
        thread_id=uuid4(),
        run_id=uuid4(),
        actor_id="test-actor",
        resources=ResourceSnapshot(
            knowledge_bases=tuple(ResourceRef("knowledge_base", code) for code in codes)
        ),
        config={},
        prompt_docs="",
        checkpointer=None,
        recorder=AuditRecorder(run_id=uuid4(), agent_slug=agent_slug),
    )


def _call(tool_name: str, context: RuntimeContext | None, **kwargs: Any) -> str:
    tool = default_registry().get(tool_name)
    runtime = SimpleNamespace(context=context)
    return asyncio.run(tool.coroutine(runtime=runtime, **kwargs))


def test_tools_fail_closed_without_runtime_context() -> None:
    assert "运行上下文缺失" in _call("query_kb", None, question="早餐几点")
    assert "运行上下文缺失" in _call("list_kbs", None)


def test_query_kb_rejects_blank_question_and_unknown_agent() -> None:
    assert "检索问题不能为空" in _call("query_kb", _context(), question="   ")
    assert "未配置知识库可见范围" in _call(
        "query_kb", _context("hotel-guest", agent_slug="housekeeping"), question="早餐几点"
    )
    assert "没有可用的知识库" in _call("query_kb", _context(), question="早餐几点")


def test_query_kb_rejects_knowledge_base_outside_the_snapshot() -> None:
    result = _call(
        "query_kb",
        _context("hotel-guest"),
        question="早餐几点",
        kb_ids=["internal-faq"],
    )
    assert "未授权检索知识库：internal-faq" in result
    assert "hotel-guest" in result


def test_retrieval_ref_collector_drains_once() -> None:
    collector = RetrievalRefCollector()
    assert collector.drain() == []
    collector.add([{"chunk_id": "c1", "index": 1}, {"chunk_id": "c2", "index": 2}])
    assert [ref["chunk_id"] for ref in collector.drain()] == ["c1", "c2"]
    assert collector.drain() == []
