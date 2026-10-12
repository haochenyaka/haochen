"""入住通知下发：支付确认后经微信订阅消息推送房号楼层、开门密码与入住指引（第二通道）。

不变量：
- best-effort：渠道失败不重试（契约 §8-11），任务结果与主通道（订单页 / 会话内下发）不受影响；
- 每次尝试都在订单事件流留下 `credential_notified` 事实（不含明文），供对账与前台外呼；
  凭证已存在（明文不可回读）时不下发也不记事件——重放不产生第二次触达；
- 明文只随本次请求载荷发往渠道：本模块的日志、事件与返回载荷都不含 code；
- 入住指引优先取自知识库，检索不可用时退回固定文案，不让通知因知识库故障而缺席。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge.retrieval import KnowledgeRetriever
from app.lock.service import CredentialFact
from app.models.domain import Order
from app.repositories import GuestRepository, KnowledgeBaseRepository, OrderRepository
from app.wechat import WechatClient, WechatError

logger = logging.getLogger(__name__)

NOTIFIED_EVENT = "credential_notified"
NOTIFY_CHANNEL = "wechat_subscribe"
NOTIFY_PAGE = "pages/orders/index"
"""点击通知打开的页面：订单列表（订单详情在 P4-T8 落地后改指详情页）。"""

GUIDE_KB_CODE = "hotel-guest"
GUIDE_QUERY = "入住须知：Wi-Fi 连接、早餐时间、退房时间"
GUIDE_FALLBACK = "Wi-Fi、早餐与退房时间等详情见小程序「订单」页；也可直接在对话里问我。"
GUIDE_CHUNK_CHARS = 80
GUIDE_MAX_CHUNKS = 2
GUIDE_MAX_CHARS = 240
REASON_MAX_CHARS = 200

_WHITESPACE = re.compile(r"\s+")
_MARKDOWN_NOISE = re.compile(r"[#*`>]+|(?<= )- ")


@dataclass(frozen=True)
class NoticeOutcome:
    """一次下发尝试的结果：status ∈ sent/failed/skipped，reason 为失败或跳过原因（不含明文）。"""

    status: str
    reason: str | None = None
    message_id: str | None = None


async def notify_checkin(
    session: AsyncSession, *, order_id: UUID, fact: CredentialFact, client: WechatClient
) -> NoticeOutcome:
    """下发入住通知并把结果写入订单事件；渠道与配置问题一律收敛为 outcome，不向上抛。"""
    if fact.code is None:
        return NoticeOutcome(status="skipped", reason="凭证已存在，明文不可回读，不重复下发")

    order = await OrderRepository(session).get_by_id(order_id)
    if order is None:
        return NoticeOutcome(status="skipped", reason="订单不存在")

    template_id = get_settings().wechat_subscribe_template_id
    guest = await GuestRepository(session).get_by_id(order.guest_id)
    if not template_id:
        outcome = NoticeOutcome(status="skipped", reason="未配置订阅消息模板")
    elif guest is None or not guest.openid:
        outcome = NoticeOutcome(status="skipped", reason="客人缺少微信身份，无法下发订阅消息")
    else:
        try:
            sent = await client.send_subscribe_message(
                openid=guest.openid,
                template_id=template_id,
                data={
                    "room_no": fact.room_no,
                    "floor": str(fact.floor),
                    "code": fact.code,
                    "guide": await _guide(session),
                },
                page=NOTIFY_PAGE,
            )
        except WechatError as exc:
            outcome = NoticeOutcome(status="failed", reason=str(exc)[:REASON_MAX_CHARS])
        else:
            outcome = NoticeOutcome(status="sent", message_id=sent.message_id)

    await _record(session, order, outcome, template_id=template_id)
    if outcome.status == "sent":
        logger.info("入住通知已下发：order_no=%s room_no=%s", order.order_no, fact.room_no)
    else:
        logger.warning(
            "入住通知未下发：order_no=%s status=%s reason=%s",
            order.order_no,
            outcome.status,
            outcome.reason,
        )
    return outcome


async def _record(
    session: AsyncSession, order: Order, outcome: NoticeOutcome, *, template_id: str
) -> None:
    """下发结果落订单事件：只记渠道与状态，不记载荷；命中并发改写时只告警。"""
    event: dict[str, object] = {
        "type": NOTIFIED_EVENT,
        "channel": NOTIFY_CHANNEL,
        "status": outcome.status,
    }
    if outcome.reason:
        event["reason"] = outcome.reason
    if template_id:
        event["template_id"] = template_id
    written = await OrderRepository(session).apply_fields(
        order.id, from_statuses=(order.status,), values={}, event=event
    )
    if not written:
        logger.warning("入住通知事件写入未命中（订单状态并发改写）：order_no=%s", order.order_no)


async def _guide(session: AsyncSession) -> str:
    """入住指引：知识库摘要优先，检索不可用或未命中时退回固定文案。"""
    try:
        kb = await KnowledgeBaseRepository(session).get_by_code(GUIDE_KB_CODE)
        if kb is None or not kb.active:
            return GUIDE_FALLBACK
        context = await KnowledgeRetriever(session).retrieve(
            GUIDE_QUERY, kb_ids=[kb.id], top_n=GUIDE_MAX_CHUNKS
        )
    except Exception:
        logger.warning("入住指引检索失败，使用兜底文案", exc_info=True)
        return GUIDE_FALLBACK
    snippets = [_snippet(chunk.content) for chunk in context.chunks]
    guide = "；".join(item for item in snippets if item)
    return guide[:GUIDE_MAX_CHARS] if guide else GUIDE_FALLBACK


def _snippet(content: str) -> str:
    """通知是纯文本字段：去掉 Markdown 标记与列表符号后再截断。"""
    cleaned = _MARKDOWN_NOISE.sub("", _WHITESPACE.sub(" ", content))
    return _WHITESPACE.sub(" ", cleaned).strip()[:GUIDE_CHUNK_CHARS]
