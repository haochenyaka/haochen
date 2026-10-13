"""工具审批用例：待审批列表、批准/拒绝决定 + 恢复投递。

决定落在 `tool_approval_requests`；批准/拒绝后把对应 interrupted Run 重新投递，
执行器 `claim_resume` 认领、图从检查点以 `Command(resume=...)` 续跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.errors import ConflictError, NotFoundError
from app.models.tool_approval import ToolApprovalRequest
from app.repositories import (
    AgentRunRepository,
    ToolApprovalRepository,
    UserRepository,
)
from app.services.run_dispatch import RunDispatcher, dispatch_run


@dataclass(frozen=True)
class ApprovalView:
    id: str
    run_id: str
    thread_id: str
    tool_name: str
    call_id: str
    reason: str | None
    args_digest: dict[str, object] | None
    status: str
    decided_by: str | None
    decided_at: datetime | None
    created_at: datetime


class ApprovalNotFoundError(NotFoundError):
    pass


class ApprovalAlreadyDecidedError(ConflictError):
    pass


class ToolApprovalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._approvals = ToolApprovalRepository(session)
        self._runs = AgentRunRepository(session)

    async def list_pending(self, *, limit: int = 100) -> list[ApprovalView]:
        rows = await self._approvals.list_pending(limit=limit)
        return [await self._view(row) for row in rows]

    async def decide(
        self,
        *,
        approval_id: UUID,
        approved: bool,
        decided_by: UUID,
        dispatcher: RunDispatcher | None = None,
    ) -> ApprovalView:
        """批准/拒绝：落决定并投递同一 Run 恢复。

        - 审批不存在 → NotFound；已决定（非 pending）→ Conflict。
        - 决定落库后、Run 重新投递前提交（投递在提交之后，与既有口径一致）。
        """
        approval = await self._approvals.get_by_id(approval_id)
        if approval is None:
            raise ApprovalNotFoundError("tool_approval", approval_id)
        if approval.status != "pending":
            raise ApprovalAlreadyDecidedError(f"审批已处理（{approval.status}），不可重复决定")
        # Run 必须仍在 interrupted 等待；否则视为状态分叉，拒绝
        run = await self._runs.get_by_id(approval.run_id)
        if run is None or run.status != "interrupted":
            raise ApprovalAlreadyDecidedError(
                f"运行不在等待审批（{run.status if run else 'missing'}），不可决定"
            )
        updated = await self._approvals.decide(
            approval_id, approved=approved, decided_by=decided_by
        )
        if updated is None:
            raise ApprovalAlreadyDecidedError("审批并发改写，决定未生效")
        await self._session.commit()
        # 提交后投递恢复：claim_resume 认领 interrupted Run
        await dispatch_run(dispatcher, approval.run_id)
        return await self._view(updated)

    async def _view(self, row: ToolApprovalRequest) -> ApprovalView:
        decided_by = row.decided_by
        decided_name: str | None = None
        if decided_by is not None:
            user = await UserRepository(self._session).get_by_id(decided_by)
            decided_name = user.username if user is not None else str(decided_by)
        return ApprovalView(
            id=str(row.id),
            run_id=str(row.run_id),
            thread_id=str(row.thread_id),
            tool_name=row.tool_name,
            call_id=row.call_id,
            reason=row.reason,
            args_digest=row.args_digest,
            status=row.status,
            decided_by=decided_name,
            decided_at=row.decided_at,
            created_at=row.created_at,
        )
