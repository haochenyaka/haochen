"""知识库咨询工具：执行处复验授权后检索，引用原文回灌模型并把引用清单留痕审计。

"模型可见"与"可执行"分离（见 registry）：模型点名的知识库必须是本次 Run 资源快照里
的那几个，且执行时重新读库确认存在、active 与可见范围匹配；任一不满足整次调用拒绝，
不静默丢弃（fail-closed，快照不是授权替代品）。命中片段来自两段式检索，正文以
PostgreSQL 的 kb_chunks 为准，因此引用编号、标题与偏移可与库内事实逐字对齐；引用清单
写入 Run 内收集器，模型调用时取走并落 model_audit.retrieval_refs。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from uuid import UUID

from langgraph.prebuilt import ToolRuntime

from app.agents.runtime import RuntimeContext
from app.agents.toolkits.registry import builtin_tool
from app.knowledge.retrieval import KnowledgeRetriever, RetrievalError
from app.repositories import KnowledgeBaseRepository

logger = logging.getLogger(__name__)

SCOPE_BY_AGENT: dict[str, str] = {"front-desk": "guest"}
"""Agent → 可见范围：客人侧 Agent 只能检索 guest 范围内的知识库。"""

MAX_TOP_N = 10
ANSWER_INSTRUCTION = (
    "以下是与问题相关的知识库资料。回答只依据资料内容，并在引用处标注编号（如 [1]）；"
    "资料未覆盖的部分如实说明，不要编造。"
)
NO_MATCH_TEXT = "知识库中没有检索到与该问题相关的资料；请如实告知客人暂无相关资料，不要编造。"


class _RejectedError(Exception):
    """工具级策略拒绝：转成模型可见的错误文本，不产生事实。"""


def _failure(reason: str) -> str:
    logger.warning("知识库工具拒绝：%s", reason)
    return f"检索失败：{reason}"


def _scope_for(ctx: RuntimeContext) -> str:
    scope = SCOPE_BY_AGENT.get(ctx.agent_slug)
    if scope is None:
        raise _RejectedError(f"当前 Agent（{ctx.agent_slug}）未配置知识库可见范围")
    return scope


def _authorized_codes(ctx: RuntimeContext) -> tuple[str, ...]:
    codes = ctx.resources.ids("knowledge_base")
    if not codes:
        raise _RejectedError("当前 Agent 没有可用的知识库")
    return codes


def _selected_codes(kb_ids: Sequence[str] | None, allowed: Sequence[str]) -> list[str]:
    """模型点名的知识库必须在快照内；缺省取全部可用库。"""
    if not kb_ids:
        return list(allowed)
    selected: list[str] = []
    for raw in kb_ids:
        code = raw.strip()
        if not code or code in selected:
            continue
        if code not in allowed:
            raise _RejectedError(f"未授权检索知识库：{code}（可用：{'、'.join(allowed)}）")
        selected.append(code)
    return selected or list(allowed)


@builtin_tool(category="knowledge", read_only=True, display_name="知识库检索")
async def query_kb(
    question: str,
    runtime: ToolRuntime[RuntimeContext],
    kb_ids: list[str] | None = None,
    top_n: int | None = None,
) -> str:
    """检索酒店知识库并返回带引用编号的资料片段。

    question：客人的问题（保留原话即可）。
    kb_ids：知识库代码列表；缺省检索本 Agent 可用的全部知识库。
    top_n：返回片段数上限（1-10）；缺省用系统默认。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，知识库检索不可用")
    question = question.strip()
    if not question:
        return _failure("检索问题不能为空")
    try:
        scope = _scope_for(ctx)
        requested = _selected_codes(kb_ids, _authorized_codes(ctx))
    except _RejectedError as exc:
        return _failure(str(exc))
    if top_n is not None:
        top_n = max(1, min(top_n, MAX_TOP_N))
    try:
        async with ctx.session_factory() as session:
            repository = KnowledgeBaseRepository(session)
            resolved: list[UUID] = []
            for code in requested:
                kb = await repository.get_by_code(code)
                if kb is None:
                    return _failure(f"知识库不存在：{code}")
                if not kb.active:
                    return _failure(f"知识库已停用：{code}")
                if kb.scope != scope:
                    return _failure(f"知识库不在可检索范围：{code}")
                resolved.append(kb.id)
            context = await KnowledgeRetriever(session).retrieve(
                question, kb_ids=resolved, top_n=top_n
            )
    except RetrievalError as exc:
        return _failure(f"检索参数不合法：{exc}")
    if context.is_empty:
        return NO_MATCH_TEXT
    ctx.retrieval_refs.add(context.citations())
    return f"{ANSWER_INSTRUCTION}\n\n{context.prompt_text}"


@builtin_tool(category="knowledge", read_only=True, display_name="知识库目录")
async def list_kbs(runtime: ToolRuntime[RuntimeContext]) -> str:
    """列出本 Agent 可检索的知识库（代码与名称）；不确定该查哪个库时先调用。"""
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，知识库目录不可用")
    try:
        scope = _scope_for(ctx)
        codes = _authorized_codes(ctx)
    except _RejectedError as exc:
        return _failure(str(exc))
    lines: list[str] = []
    async with ctx.session_factory() as session:
        repository = KnowledgeBaseRepository(session)
        for code in codes:
            kb = await repository.get_by_code(code)
            if kb is None or not kb.active or kb.scope != scope:
                continue
            description = f"（{kb.description}）" if kb.description else ""
            lines.append(f"- {kb.code}：{kb.name}{description}")
    if not lines:
        return "当前没有可检索的知识库。"
    return "当前可检索的知识库（查询时把代码传给 query_kb 的 kb_ids）：\n" + "\n".join(lines)
