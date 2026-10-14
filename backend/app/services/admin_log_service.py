"""智能体日志管理用例：跨用户列表（含客人画像）、批量软删与留痕。

口径（术语表 §15）：
- 日志列表默认不展示软删行（include_deleted=True 时恢复）；
- 软删不影响报表（report_service 不查询 deleted_at）；
- 删除操作写 admin_action_log 留痕（谁、何时、目标 id、数量）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, String, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_log import AdminActionLog
from app.models.audit import ModelAudit, ToolAudit
from app.models.domain import Guest
from app.models.runtime import AgentRun, User
from app.repositories.support import affected_rows

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class LogEntry:
    kind: str
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
    retrieval_ref_count: int | None = None
    tool_name: str | None = None
    args_digest: dict[str, object] | None = None
    idempotency_key: str | None = None
    deleted_at: datetime | None = None
    guest_username: str | None = None
    guest_nickname: str | None = None


@dataclass
class LogPage:
    entries: list[LogEntry]
    total: int
    page: int
    page_size: int
    has_more: bool


@dataclass
class SoftDeleteResult:
    model_affected: int
    tool_affected: int
    total_affected: int
    log_id: int


class AdminLogService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_logs(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        kind: str | None = None,
        agent_slug: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> LogPage:
        limit = min(page_size, MAX_PAGE_SIZE)
        offset = (max(page, 1) - 1) * limit

        model_query = self._build_model_query(
            start=start,
            end=end,
            agent_slug=agent_slug,
            status=status,
            run_id=run_id,
            include_deleted=include_deleted,
        )
        tool_query = self._build_tool_query(
            start=start,
            end=end,
            agent_slug=agent_slug,
            status=status,
            run_id=run_id,
            include_deleted=include_deleted,
        )

        from_model: list[LogEntry] = []
        from_tool: list[LogEntry] = []

        if kind != "tool":
            model_rows: Sequence[ModelAudit] = (
                (await self._session.execute(model_query.limit(limit + 1).offset(offset)))
                .scalars()
                .all()
            )
            from_model = [self._to_log_entry(row) for row in model_rows]

        if kind != "model":
            tool_rows: Sequence[ToolAudit] = (
                (await self._session.execute(tool_query.limit(limit + 1).offset(offset)))
                .scalars()
                .all()
            )
            from_tool = [self._to_tool_entry(row) for row in tool_rows]

        combined = sorted(from_model + from_tool, key=lambda e: e.created_at, reverse=True)

        # Get total count
        model_total = 0 if kind == "tool" else await self._count(model_query)
        tool_total = 0 if kind == "model" else await self._count(tool_query)
        total = model_total + tool_total

        # Enrich with guest info
        combined = combined[:limit]
        await self._enrich_guests(combined)

        return LogPage(
            entries=combined,
            total=total,
            page=page,
            page_size=limit,
            has_more=(offset + limit) < total,
        )

    async def soft_delete(
        self,
        *,
        model_ids: list[int] | None = None,
        tool_ids: list[int] | None = None,
        actor_user_id: UUID,
        actor_username: str,
        actor_role: str,
    ) -> SoftDeleteResult:
        now = datetime.now()
        model_affected = 0
        tool_affected = 0
        target_ids: list[object] = []

        if model_ids:
            result = await self._session.execute(
                update(ModelAudit)
                .where(ModelAudit.id.in_(model_ids), ModelAudit.deleted_at.is_(None))
                .values(deleted_at=now)
                .execution_options(synchronize_session=False)
            )
            model_affected = affected_rows(result)
            target_ids.extend(model_ids)

        if tool_ids:
            result = await self._session.execute(
                update(ToolAudit)
                .where(ToolAudit.id.in_(tool_ids), ToolAudit.deleted_at.is_(None))
                .values(deleted_at=now)
                .execution_options(synchronize_session=False)
            )
            tool_affected = affected_rows(result)
            target_ids.extend(tool_ids)

        total = model_affected + tool_affected

        log = AdminActionLog(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            actor_role=actor_role,
            action="soft_delete_audit",
            target_kind="audit",
            target_ids=target_ids,
            target_count=total,
            detail={"model_ids": model_ids, "tool_ids": tool_ids},
        )
        self._session.add(log)
        await self._session.flush()
        assert log.id is not None

        return SoftDeleteResult(
            model_affected=model_affected,
            tool_affected=tool_affected,
            total_affected=total,
            log_id=log.id,
        )

    def _build_model_query(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        agent_slug: str | None,
        status: str | None,
        run_id: str | None,
        include_deleted: bool,
    ) -> Select:
        stmt = select(ModelAudit).order_by(ModelAudit.created_at.desc(), ModelAudit.id.desc())
        conditions: list[Any] = []
        if not include_deleted:
            conditions.append(ModelAudit.deleted_at.is_(None))
        if start is not None:
            conditions.append(ModelAudit.created_at >= start)
        if end is not None:
            conditions.append(ModelAudit.created_at < end)
        if agent_slug is not None:
            conditions.append(ModelAudit.agent_slug == agent_slug)
        if status is not None:
            conditions.append(ModelAudit.status == status)
        if run_id is not None:
            conditions.append(ModelAudit.run_id == run_id)
        if conditions:
            stmt = stmt.where(*conditions)
        return stmt

    def _build_tool_query(
        self,
        *,
        start: datetime | None,
        end: datetime | None,
        agent_slug: str | None,
        status: str | None,
        run_id: str | None,
        include_deleted: bool,
    ) -> Select:
        stmt = select(ToolAudit).order_by(ToolAudit.created_at.desc(), ToolAudit.id.desc())
        conditions: list[Any] = []
        if not include_deleted:
            conditions.append(ToolAudit.deleted_at.is_(None))
        if start is not None:
            conditions.append(ToolAudit.created_at >= start)
        if end is not None:
            conditions.append(ToolAudit.created_at < end)
        if agent_slug is not None:
            conditions.append(ToolAudit.agent_slug == agent_slug)
        if status is not None:
            conditions.append(ToolAudit.status == status)
        if run_id is not None:
            conditions.append(ToolAudit.run_id == run_id)
        if conditions:
            stmt = stmt.where(*conditions)
        return stmt

    async def _count(self, query: Select) -> int:
        count_q = select(func.count()).select_from(query.subquery())
        return int((await self._session.execute(count_q)).scalar_one())

    def _to_log_entry(self, row: ModelAudit) -> LogEntry:
        return LogEntry(
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
            retrieval_ref_count=len(row.retrieval_refs) if row.retrieval_refs else None,
            deleted_at=row.deleted_at,
        )

    def _to_tool_entry(self, row: ToolAudit) -> LogEntry:
        return LogEntry(
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

    async def _enrich_guests(self, entries: list[LogEntry]) -> None:
        """从 agent_runs → users/guests 批量回填客人身份。"""
        run_ids = list({e.run_id for e in entries if e.run_id})
        if not run_ids:
            return
        rows = (
            await self._session.execute(
                select(cast(AgentRun.id, String).label("run_id"), AgentRun.user_id)
                .where(cast(AgentRun.id, String).in_(run_ids))
                .distinct()
            )
        ).all()
        user_ids = list({row.user_id for row in rows})
        if not user_ids:
            return

        user_rows = (
            await self._session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
        ).all()
        username_by_user = {str(row.id): row.username for row in user_rows}

        guest_rows = (
            await self._session.execute(
                select(Guest.user_id, Guest.nickname).where(Guest.user_id.in_(user_ids))
            )
        ).all()
        nickname_by_user: dict[str, str | None] = {
            str(row.user_id): row.nickname for row in guest_rows
        }

        run_user: dict[str, tuple[str | None, str | None]] = {}
        for row in rows:
            uid = str(row.user_id)
            run_user[str(row.run_id)] = (username_by_user.get(uid), nickname_by_user.get(uid))

        for entry in entries:
            if entry.run_id in run_user:
                username, nickname = run_user[entry.run_id]
                object.__setattr__(entry, "guest_username", username)
                object.__setattr__(entry, "guest_nickname", nickname)

    async def get_action_logs(
        self, *, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE
    ) -> list[AdminActionLog]:
        limit = min(page_size, MAX_PAGE_SIZE)
        offset = (max(page, 1) - 1) * limit
        rows = (
            (
                await self._session.execute(
                    select(AdminActionLog)
                    .order_by(AdminActionLog.created_at.desc(), AdminActionLog.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


__all__ = [
    "AdminLogService",
    "LogEntry",
    "LogPage",
    "SoftDeleteResult",
]
