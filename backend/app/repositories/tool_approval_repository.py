"""工具审批请求仓储：挂起事实的落库、待审批列表、决定流转。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_approval import ToolApprovalRequest
from app.repositories.support import affected_rows


class ToolApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_pending(
        self,
        *,
        run_id: UUID,
        thread_id: UUID,
        call_id: str,
        tool_name: str,
        reason: str | None,
        args_digest: dict[str, object] | None,
    ) -> ToolApprovalRequest:
        row = ToolApprovalRequest(
            run_id=run_id,
            thread_id=thread_id,
            call_id=call_id,
            tool_name=tool_name,
            reason=reason,
            args_digest=args_digest,
            status="pending",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_by_id(self, approval_id: UUID) -> ToolApprovalRequest | None:
        stmt = select(ToolApprovalRequest).where(ToolApprovalRequest.id == approval_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_pending_for_run(self, run_id: UUID) -> ToolApprovalRequest | None:
        stmt = (
            select(ToolApprovalRequest)
            .where(ToolApprovalRequest.run_id == run_id, ToolApprovalRequest.status == "pending")
            .order_by(ToolApprovalRequest.created_at)
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_decided_for_run(self, run_id: UUID) -> ToolApprovalRequest | None:
        """最近一条已决定（approved/denied）的审批：恢复入口据此构造 resume。"""
        stmt = (
            select(ToolApprovalRequest)
            .where(
                ToolApprovalRequest.run_id == run_id,
                ToolApprovalRequest.status.in_(("approved", "denied")),
            )
            .order_by(ToolApprovalRequest.decided_at.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_pending(self, *, limit: int = 100) -> list[ToolApprovalRequest]:
        stmt = (
            select(ToolApprovalRequest)
            .where(ToolApprovalRequest.status == "pending")
            .order_by(ToolApprovalRequest.created_at)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def decide(
        self, approval_id: UUID, *, approved: bool, decided_by: UUID
    ) -> ToolApprovalRequest | None:
        """pending → approved/denied（仅当仍为 pending）；返回更新后的行或 None。"""
        target = "approved" if approved else "denied"
        result = await self._session.execute(
            update(ToolApprovalRequest)
            .where(ToolApprovalRequest.id == approval_id, ToolApprovalRequest.status == "pending")
            .values(status=target, decided_by=decided_by, decided_at=func.now())
            .execution_options(synchronize_session=False)
        )
        if affected_rows(result) != 1:
            return None
        return await self.get_by_id(approval_id)

    async def mark_consumed(self, approval_id: UUID) -> bool:
        """决定已被图消费：resume 成功后置 consumed，避免重复恢复。"""
        result = await self._session.execute(
            update(ToolApprovalRequest)
            .where(
                ToolApprovalRequest.id == approval_id,
                ToolApprovalRequest.status.in_(("approved", "denied")),
            )
            .values(status="consumed", consumed_at=func.now())
            .execution_options(synchronize_session=False)
        )
        return affected_rows(result) == 1

    async def list_for_run(self, run_id: UUID) -> Sequence[ToolApprovalRequest]:
        stmt = (
            select(ToolApprovalRequest)
            .where(ToolApprovalRequest.run_id == run_id)
            .order_by(ToolApprovalRequest.created_at)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())
