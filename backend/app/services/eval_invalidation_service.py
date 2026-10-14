"""评估报告自动作废服务：知识库内容变更时触发旧评估基线作废。

不变量：
- 内容或配置变化后旧报告的 recall/precision 不可直接比较，强制标记作废；
- 作废是条件更新：只作用于尚未作废的报告，已有 superseded_by 的行不重复标记；
- 作废不产生新评估报告，只标记旧报告为不可比较状态。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.eval_repository import KbEvalReportRepository

logger = logging.getLogger(__name__)


async def invalidate_old_reports_on_content_change(
    session: AsyncSession,
    *,
    reason: str = "content_changed",
) -> int:
    """知识库内容变更时，将所有尚未作废的报告标记为已作废。

    返回被作废的报告数。新评估运行时 `persist_report` 会基于最新内容指纹
    创建新报告，旧报告因已被标记而不再参与可比性判定。
    """
    repo = KbEvalReportRepository(session)
    recent = await repo.list_recent(limit=100)
    count = 0
    for report in recent:
        if report.superseded_by is not None:
            continue
        ok = await repo.mark_superseded(report.id, superseded_by=report.id, reason=reason)
        if ok:
            count += 1
    if count:
        logger.info("KB 内容变更：已作废 %d 份旧评估报告（原因：%s）", count, reason)
    return count
