"""提示词文档装载：AGENTS.md / USER.md 各 64 KiB 上限，超限截断并留痕。"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from app.domain.vocabulary import HOTEL_TIMEZONE, WEEKDAY_LABELS

logger = logging.getLogger(__name__)

PROMPT_DOC_FILES = ("AGENTS.md", "USER.md")
PROMPT_DOC_MAX_BYTES = 64 * 1024


def runtime_context_prompt() -> str:
    """运行期上下文补丁：装配期注入酒店当地当天日期。

    supervisor 图每次 Run 重建，此段随之刷新，相对日期（"今天/明天/本周"）由模型据此推算，
    不依赖各子 Agent 各自持有 get_current_time。
    """
    now = datetime.now(HOTEL_TIMEZONE)
    return (
        "# 运行期上下文\n"
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')} "
        f"星期{WEEKDAY_LABELS[now.weekday()]}（酒店所在地 UTC+8）。\n"
        "涉及“今天、明天、后天、本周”等相对日期时，以此刻推算具体日期，不要凭记忆。"
    )


def load_prompt_docs(directory: Path) -> str:
    """读取目录下各级提示词文档（缺失即跳过），按固定顺序拼成一段补充提示词。"""
    sections: list[str] = []
    for filename in PROMPT_DOC_FILES:
        path = directory / filename
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if len(raw) > PROMPT_DOC_MAX_BYTES:
            logger.warning(
                "prompt doc truncated: path=%s bytes=%d limit=%d",
                path,
                len(raw),
                PROMPT_DOC_MAX_BYTES,
            )
            raw = raw[:PROMPT_DOC_MAX_BYTES]
        text = raw.decode("utf-8", errors="replace").strip()
        if text:
            sections.append(f"# {filename}\n{text}")
    return "\n\n".join(sections)
