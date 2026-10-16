"""转人工工具：把当前会话转交人工客服，生成待处理工单（复用会话侧转人工链路）。

工单按线程幂等（已有 pending 即复用，不产生重复工单），因此本工具不另造幂等键；标为写类
（read_only=False）使 ToolGuard 在单个 Run 内对同参重复点名做复用，两层幂等各司其职。
"""

from __future__ import annotations

import logging

from langgraph.prebuilt import ToolRuntime

from app.agents.runtime import RuntimeContext
from app.agents.toolkits.registry import builtin_tool
from app.services.agent_request_queue_service import QueueServiceError
from app.services.conversation_service import ConversationService

logger = logging.getLogger(__name__)


def _failure(reason: str) -> str:
    logger.warning("转人工工具失败：%s", reason)
    return f"操作失败：{reason}"


@builtin_tool(category="handoff", display_name="转人工", read_only=False)
async def request_handoff(reason: str, runtime: ToolRuntime[RuntimeContext]) -> str:
    """把当前会话转交人工客服并生成待处理工单；同一会话已有待处理工单时复用。

    Args:
        reason: 转人工原因，简述客人诉求（如"申请退款到账""投诉房间噪音"）。
    """
    ctx = runtime.context
    if ctx is None:
        return _failure("运行上下文缺失，转人工不可用")
    if not reason:
        return "请说明需要转人工的原因。"
    try:
        async with ctx.session_factory() as session:
            outcome = await ConversationService(session).request_handoff(
                thread_id=ctx.thread_id, user_id=ctx.user_id, reason=reason
            )
            await session.commit()
    except QueueServiceError as exc:
        return _failure(str(exc))
    if outcome.created:
        return "已为您转接人工客服，工单已生成，客服会尽快与您联系。"
    return "已为您转接人工客服：本会话此前已有待处理工单，客服会尽快与您联系。"
