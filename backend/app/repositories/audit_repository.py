"""审计事实仓储：Model/Tool 生命周期写入与统一读接口。

seq 冲突显式失败（不静默重排）；读接口统一为一条时间线，最新 500 条 + truncated。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ModelAudit, RouteAudit, ToolAudit
from app.models.runtime import AgentRun
from app.repositories.support import affected_rows

AuditModel = type[ModelAudit] | type[ToolAudit] | type[RouteAudit]

AUDIT_READ_LIMIT = 500


@dataclass(frozen=True)
class AuditEntry:
    kind: Literal["model", "tool"]
    id: int
    run_id: str
    seq: int
    agent_slug: str
    status: str
    created_at: datetime
    duration_ms: int | None = None
    error: str | None = None
    model: str | None = None
    prompt_tokens: int | None = None
    cached_prompt_tokens: int | None = None
    completion_tokens: int | None = None
    retrieval_refs: list[dict[str, object]] | None = None
    tool_name: str | None = None
    args_digest: dict[str, object] | None = None
    idempotency_key: str | None = None
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class AuditTimeline:
    entries: list[AuditEntry]
    truncated: bool


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _next_seq(self, model: AuditModel, run_key: str) -> int:
        stmt = select(func.coalesce(func.max(model.seq), 0) + 1).where(model.run_id == run_key)
        return int((await self._session.execute(stmt)).scalar_one())

    async def append_model(
        self,
        *,
        run_id: UUID | str,
        agent_slug: str,
        actor_type: str,
        actor_id: str,
        model: str,
        status: str,
        seq: int | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        prompt_tokens: int | None = None,
        cached_prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        retrieval_refs: list[dict[str, object]] | None = None,
    ) -> ModelAudit:
        run_key = str(run_id)
        audit = ModelAudit(
            run_id=run_key,
            seq=seq if seq is not None else await self._next_seq(ModelAudit, run_key),
            agent_slug=agent_slug,
            actor_type=actor_type,
            actor_id=actor_id,
            model=model,
            status=status,
            error=error,
            duration_ms=duration_ms,
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            completion_tokens=completion_tokens,
            retrieval_refs=retrieval_refs,
        )
        self._session.add(audit)
        await self._session.flush()
        return audit

    async def append_tool(
        self,
        *,
        run_id: UUID | str,
        agent_slug: str,
        tool_name: str,
        status: str,
        seq: int | None = None,
        args_digest: dict[str, object] | None = None,
        idempotency_key: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
    ) -> ToolAudit:
        run_key = str(run_id)
        audit = ToolAudit(
            run_id=run_key,
            seq=seq if seq is not None else await self._next_seq(ToolAudit, run_key),
            agent_slug=agent_slug,
            tool_name=tool_name,
            status=status,
            args_digest=args_digest,
            idempotency_key=idempotency_key,
            duration_ms=duration_ms,
            error=error,
        )
        self._session.add(audit)
        await self._session.flush()
        return audit

    async def append_route(
        self,
        *,
        run_id: UUID | str,
        agent_slug: str,
        decided_route: str,
        source: str,
        seq: int | None = None,
        dispatched_route: str | None = None,
        reason: str | None = None,
    ) -> RouteAudit:
        run_key = str(run_id)
        audit = RouteAudit(
            run_id=run_key,
            seq=seq if seq is not None else await self._next_seq(RouteAudit, run_key),
            agent_slug=agent_slug,
            decided_route=decided_route,
            dispatched_route=dispatched_route,
            source=source,
            reason=reason,
        )
        self._session.add(audit)
        await self._session.flush()
        return audit

    async def list_routes(self, run_id: UUID | str) -> list[RouteAudit]:
        """某 Run 的路由决策事实，按 seq 升序（决策先后即派发先后）。"""
        stmt = (
            select(RouteAudit)
            .where(RouteAudit.run_id == str(run_id), RouteAudit.deleted_at.is_(None))
            .order_by(RouteAudit.seq)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_run_reconciled(self, run_id: UUID | str) -> int:
        """终态 State 对账证明：标记该 run 的模型审计行已对齐终态。"""
        result = await self._session.execute(
            update(ModelAudit)
            .where(ModelAudit.run_id == str(run_id))
            .values(state_reconciled=True)
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result)

    async def retrieval_refs_by_run(
        self, run_ids: Sequence[UUID | str]
    ) -> dict[str, list[dict[str, object]]]:
        """每个 Run 最后一次模型调用的引用清单（历史消息重建引用展示的口径）。

        与 model_audit 写入口径一致：形成最终答复的是该 Run 的最后一次模型调用，
        未引用知识库（refs 为空）或行数不足的 Run 不出现在结果里。
        """
        keys = [str(run_id) for run_id in run_ids]
        if not keys:
            return {}
        stmt = (
            select(ModelAudit)
            .where(ModelAudit.run_id.in_(keys))
            .order_by(ModelAudit.run_id, ModelAudit.seq)
        )
        refs: dict[str, list[dict[str, object]]] = {}
        for row in (await self._session.execute(stmt)).scalars():
            if row.retrieval_refs:
                refs[row.run_id] = row.retrieval_refs
        return refs

    async def list_timeline(
        self,
        *,
        run_id: UUID | None = None,
        thread_id: UUID | None = None,
        limit: int = AUDIT_READ_LIMIT,
        include_deleted: bool = False,
    ) -> AuditTimeline:
        """Model/Tool 合并时间线（倒序）；run_id 与 thread_id 必须且只能提供一个。"""
        if (run_id is None) == (thread_id is None):
            raise ValueError("run_id 与 thread_id 必须且只能提供一个")
        if run_id is not None:
            run_keys = [str(run_id)]
        else:
            assert thread_id is not None
            run_keys = [
                str(value)
                for value in (
                    await self._session.execute(
                        select(AgentRun.id).where(AgentRun.thread_id == thread_id)
                    )
                ).scalars()
            ]
        if not run_keys:
            return AuditTimeline(entries=[], truncated=False)

        probe = limit + 1
        base = [] if include_deleted else [ModelAudit.deleted_at.is_(None)]
        model_rows = (
            (
                await self._session.execute(
                    select(ModelAudit)
                    .where(ModelAudit.run_id.in_(run_keys), *base)
                    .order_by(ModelAudit.created_at.desc(), ModelAudit.id.desc())
                    .limit(probe)
                )
            )
            .scalars()
            .all()
        )
        tool_base = [] if include_deleted else [ToolAudit.deleted_at.is_(None)]
        tool_rows = (
            (
                await self._session.execute(
                    select(ToolAudit)
                    .where(ToolAudit.run_id.in_(run_keys), *tool_base)
                    .order_by(ToolAudit.created_at.desc(), ToolAudit.id.desc())
                    .limit(probe)
                )
            )
            .scalars()
            .all()
        )
        entries = [
            AuditEntry(
                kind="model",
                id=row.id,
                run_id=row.run_id,
                seq=row.seq,
                agent_slug=row.agent_slug,
                status=row.status,
                created_at=row.created_at,
                duration_ms=row.duration_ms,
                error=row.error,
                model=row.model,
                prompt_tokens=row.prompt_tokens,
                cached_prompt_tokens=row.cached_prompt_tokens,
                completion_tokens=row.completion_tokens,
                retrieval_refs=row.retrieval_refs,
                deleted_at=row.deleted_at,
            )
            for row in model_rows
        ] + [
            AuditEntry(
                kind="tool",
                id=row.id,
                run_id=row.run_id,
                seq=row.seq,
                agent_slug=row.agent_slug,
                status=row.status,
                created_at=row.created_at,
                duration_ms=row.duration_ms,
                error=row.error,
                tool_name=row.tool_name,
                args_digest=row.args_digest,
                idempotency_key=row.idempotency_key,
                deleted_at=row.deleted_at,
            )
            for row in tool_rows
        ]
        entries.sort(key=lambda entry: (entry.created_at, entry.kind, entry.id), reverse=True)
        return AuditTimeline(entries=entries[:limit], truncated=len(entries) > limit)
