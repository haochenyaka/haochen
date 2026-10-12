"""评估仓储：报告追加写入与作废标记；历史指标一律不改写。

不变量：
- 读取一律 `populate_existing`，避免 identity map 回放旧状态；
- 作废是条件更新：只把尚未作废（superseded_by 为空）的报告标记一次，原因不被覆盖。
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.eval import KbEvalReport
from app.repositories.support import affected_rows


class KbEvalReportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        dataset_path: str,
        dataset_fingerprint: str,
        content_fingerprint: str,
        config_fingerprint: str,
        k: int,
        questions: int,
        evaluator: str | None,
        retrieval_config: dict[str, object],
        metrics: dict[str, object],
        outcomes: Sequence[dict[str, object]],
    ) -> KbEvalReport:
        report = KbEvalReport(
            dataset_path=dataset_path,
            dataset_fingerprint=dataset_fingerprint,
            content_fingerprint=content_fingerprint,
            config_fingerprint=config_fingerprint,
            k=k,
            questions=questions,
            evaluator=evaluator,
            retrieval_config=dict(retrieval_config),
            metrics=dict(metrics),
            outcomes=[dict(item) for item in outcomes],
        )
        self._session.add(report)
        await self._session.flush()
        return report

    async def get(self, report_id: int) -> KbEvalReport | None:
        return await self._session.get(KbEvalReport, report_id, populate_existing=True)

    async def list_by_dataset(
        self, dataset_fingerprint: str, *, limit: int = 100
    ) -> list[KbEvalReport]:
        """同数据集的全部报告（旧→新）：可比性与作废判定的作用域。"""
        stmt = (
            select(KbEvalReport)
            .where(KbEvalReport.dataset_fingerprint == dataset_fingerprint)
            .order_by(KbEvalReport.created_at, KbEvalReport.id)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def list_recent(self, *, limit: int = 20) -> list[KbEvalReport]:
        stmt = (
            select(KbEvalReport)
            .order_by(KbEvalReport.created_at.desc(), KbEvalReport.id.desc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list((await self._session.execute(stmt)).scalars())

    async def mark_superseded(self, report_id: int, *, superseded_by: int, reason: str) -> bool:
        """条件作废：只作用于尚未作废的报告，返回是否命中。"""
        stmt = (
            update(KbEvalReport)
            .where(KbEvalReport.id == report_id, KbEvalReport.superseded_by.is_(None))
            .values(superseded_by=superseded_by, invalidated_reason=reason)
        )
        return affected_rows(await self._session.execute(stmt)) == 1
