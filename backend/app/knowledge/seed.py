"""知识库样例种子：幂等创建宾客知识库、上传样例文档，提交后投递解析/索引任务。

运行：`python -m app.knowledge.seed`（可重复执行：按 (kb_id, checksum) 判存）。
样例内容在 `app/knowledge/samples/`，替换文件内容后重跑会作为新文档上传。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionFactory
from app.knowledge.parsing import source_type_for_filename
from app.knowledge.upload_service import KnowledgeUploadService
from app.repositories import KbDocumentRepository, KnowledgeBaseRepository
from app.services.task_dispatch import (
    ArqTaskDispatcher,
    NullTaskDispatcher,
    TaskDispatcher,
    dispatch_task,
)
from app.services.task_service import TaskSubmission

logger = logging.getLogger(__name__)

SAMPLES_DIR = Path(__file__).parent / "samples"
SAMPLE_README = "README.md"
KB_CODE = "hotel-guest"
KB_NAME = "皓晨酒店宾客知识库"
KB_DESCRIPTION = "样例知识库：内容为占位样例，替换为真实资料后重新上传"

SAMPLE_TITLES: dict[str, str] = {
    "hotel-intro.md": "【样例】酒店介绍与设施",
    "wifi-and-devices.md": "【样例】网络与智能设备",
    "breakfast-and-dining.md": "【样例】早餐与餐饮",
    "stay-policies.md": "【样例】入住政策与押金",
    "housekeeping-and-services.md": "【样例】客房服务与清洁",
    "faq.md": "【样例】常见问题",
    "booking-and-hold.md": "【样例】预订与保留规则",
    "reschedule-and-cancel.md": "【样例】改期与取消规则",
    "group-and-longstay.md": "【样例】团体与长住预订",
    "invoice-and-company.md": "【样例】发票与企业结算",
    "parking-and-pickup.md": "【样例】停车与接送服务",
    "meeting-room.md": "【样例】会议室与活动场地",
    "accessibility-family.md": "【样例】无障碍与亲子设施",
    "safety-and-emergency.md": "【样例】安全与应急指引",
    "membership.md": "【样例】会员与积分",
    "complaint-and-recovery.md": "【样例】投诉与补偿标准",
    "lost-and-found.md": "【样例】失物招领",
    "nearby-attractions.md": "【样例】周边配套与景点",
    "dietary-allergy.md": "【样例】特殊饮食与过敏须知",
    "eco-preference.md": "【样例】环保与清洁偏好",
}


@dataclass
class KnowledgeSeedReport:
    kb_created: bool = False
    uploaded: list[str] = field(default_factory=list)
    submissions: list[TaskSubmission] = field(default_factory=list)
    skipped: int = 0


async def seed_knowledge(session: AsyncSession) -> KnowledgeSeedReport:
    report = KnowledgeSeedReport()
    knowledge_bases = KnowledgeBaseRepository(session)
    kb = await knowledge_bases.get_by_code(KB_CODE)
    if kb is None:
        kb = await knowledge_bases.create(
            code=KB_CODE, name=KB_NAME, description=KB_DESCRIPTION, scope="guest"
        )
        report.kb_created = True
        await session.commit()

    documents = KbDocumentRepository(session)
    service = KnowledgeUploadService(session)
    for path in sorted(SAMPLES_DIR.glob("*.md")):
        if path.name == SAMPLE_README:
            continue
        source_type = source_type_for_filename(path.name)
        if source_type is None:
            logger.warning("跳过未知类型样例文件：%s", path.name)
            continue
        data = path.read_bytes()
        checksum = hashlib.sha256(data).hexdigest()
        if await documents.get_by_checksum(kb_id=kb.id, checksum=checksum) is not None:
            report.skipped += 1
            continue
        outcome = await service.upload(
            kb_id=kb.id,
            title=SAMPLE_TITLES.get(path.name, path.stem),
            source_type=source_type,
            filename=path.name,
            data=data,
        )
        report.uploaded.append(outcome.document.title)
        report.submissions.append(outcome.submission)
    return report


async def _create_dispatcher() -> TaskDispatcher:
    try:
        return await ArqTaskDispatcher.create()
    except Exception as exc:
        logger.warning("任务队列不可用，文档保持待处理，由 worker 启动补发：%s", exc)
        return NullTaskDispatcher()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dispatcher = await _create_dispatcher()
    try:
        async with SessionFactory() as session:
            report = await seed_knowledge(session)
        for submission in report.submissions:
            await dispatch_task(dispatcher, submission.task_id)
    finally:
        await dispatcher.aclose()
    logger.info(
        "知识库种子完成：知识库新建=%s 新上传=%d 幂等跳过=%d",
        report.kb_created,
        len(report.uploaded),
        report.skipped,
    )
    for title in report.uploaded:
        logger.info("已投递解析任务：%s", title)


if __name__ == "__main__":
    asyncio.run(main())
