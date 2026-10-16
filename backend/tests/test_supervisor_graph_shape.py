"""supervisor 多层图形态：拓扑可从编译图渲染，且与 docs/agent-topology.md 保持同步。

不依赖外部服务：用开发态 stub 模型与空资源快照装配图，只检查节点/边与文档中的 mermaid。
文档里的图是"生成的产物"，改图后需按文档末尾的命令重新生成，本测试即为漂移守卫。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from langgraph.graph import END

from app.agents.context import ResourceSnapshot
from app.agents.models import DevStubChatModel
from app.agents.runtime import RuntimeContext
from app.agents.supervisor.assembly import build_agent_pipeline
from app.agents.supervisor.graph import build_supervisor_graph
from app.agents.supervisor.routing import SUB_AGENT_ROUTES
from app.db import SessionFactory
from app.services.audit_service import AuditRecorder

TOPOLOGY_DOC = Path(__file__).resolve().parents[2] / "docs" / "agent-topology.md"

_EDGE_RE = re.compile(r"([A-Za-z_]\w*)\s*(?:-\.->|-->|==>)\s*([A-Za-z_]\w*)")
_NODE_RE = re.compile(r"^([A-Za-z_]\w*)\s*[(\[]")
_IGNORED_PREFIXES = ("---", "graph ", "config:", "flowchart:", "curve:", "classDef", "%%")


def _drawn_graph() -> Any:
    async def build() -> Any:
        runtime = RuntimeContext(
            agent_slug="front-desk",
            user_id=uuid4(),
            thread_id=uuid4(),
            run_id=uuid4(),
            actor_id="topology",
            resources=ResourceSnapshot(),
            config={},
            prompt_docs="",
            checkpointer=None,
            recorder=AuditRecorder(
                run_id=uuid4(), agent_slug="front-desk", session_factory=SessionFactory
            ),
        )
        return await build_supervisor_graph(
            runtime, model=DevStubChatModel(), pipeline=build_agent_pipeline(runtime)
        )

    return asyncio.run(build()).get_graph()


def _mermaid_block(text: str) -> str:
    marker = "```mermaid"
    body = text[text.index(marker) + len(marker) :]
    return body[: body.index("```")]


def _shape(mermaid: str) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """从 mermaid 提取节点与边（只认标识符，忽略样式与配置行），用于跨来源比对。"""
    nodes: set[str] = set()
    edges: set[tuple[str, str]] = set()
    for raw in mermaid.splitlines():
        line = raw.strip()
        if not line or line.startswith(_IGNORED_PREFIXES):
            continue
        pairs = [match.groups() for match in _EDGE_RE.finditer(line)]
        if pairs:
            edges.update((source, target) for source, target in pairs)
            continue
        node = _NODE_RE.match(line)
        if node:
            nodes.add(node.group(1))
    return frozenset(nodes), frozenset(edges)


def test_supervisor_graph_is_multi_layer() -> None:
    drawn = _drawn_graph()
    nodes = {node.id for node in drawn.nodes.values()}
    assert {"supervisor", *SUB_AGENT_ROUTES} <= nodes
    edges = {(edge.source, edge.target, bool(edge.conditional)) for edge in drawn.edges}
    assert ("__start__", "supervisor", False) in edges
    assert ("supervisor", END, True) in edges
    for route in SUB_AGENT_ROUTES:
        assert ("supervisor", route, True) in edges
        assert (route, "supervisor", False) in edges


def test_topology_doc_matches_compiled_graph() -> None:
    documented = _shape(_mermaid_block(TOPOLOGY_DOC.read_text(encoding="utf-8")))
    rendered = _shape(_drawn_graph().draw_mermaid())
    assert documented == rendered, "docs/agent-topology.md 与编译图不一致，请按文档命令重新生成"
