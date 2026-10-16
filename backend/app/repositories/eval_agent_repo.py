"""智能体评估仓储：报告追加写入与作废标记。

不变量：
- 读取一律 populate_existing，避免 identity map 回放旧状态；
- 作废是条件更新：只把尚未作废的报告标记一次。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eval_agent import AgentEvalReport
from app.repositories.support import affected_rows


class AgentEvalReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        dataset_path: str,
        dataset_fingerprint: str,
        config_fingerprint: str,
        model: str | None,
        total_cases: int,
        metrics: dict[str, object],
        outcomes: Sequence[dict[str, object]],
    ) -> AgentEvalReport:
        report = AgentEvalReport(
            dataset_path=dataset_path,
            dataset_fingerprint=dataset_fingerprint,
            config_fingerprint=config_fingerprint,
            model=model,
            total_cases=total_cases,
            metrics=dict(metrics),
            outcomes=[dict(item) for item in outcomes],
        )
        self._session.add(report)
        await self._session.flush()
        return report

    async def get(self, report_id: int) -> AgentEvalReport | None:
        return await self._session.get(AgentEvalReport, report_id, populate_existing=True)

    async def list_by_dataset(
        self, dataset_fingerprint: str, *, limit: int = 100
    ) -> list[AgentEvalReport]:
        stmt = (
            select(AgentEvalReport)
            .where(AgentEvalReport.dataset_fingerprint == dataset_fingerprint)
            .order_by(AgentEvalReport.created_at, AgentEvalReport.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_recent(self, *, limit: int = 20) -> list[AgentEvalReport]:
        stmt = (
            select(AgentEvalReport)
            .order_by(AgentEvalReport.created_at.desc(), AgentEvalReport.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_superseded(self, report_id: int, *, superseded_by: int, reason: str) -> bool:
        stmt = (
            update(AgentEvalReport)
            .where(
                AgentEvalReport.id == report_id,
                AgentEvalReport.superseded_by.is_(None),
            )
            .values(superseded_by=superseded_by, invalidated_reason=reason)
        )
        return affected_rows(await self._session.execute(stmt)) == 1
