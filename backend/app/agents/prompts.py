"""提示词文档装载：AGENTS.md / USER.md 各 64 KiB 上限，超限截断并留痕。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPT_DOC_FILES = ("AGENTS.md", "USER.md")
PROMPT_DOC_MAX_BYTES = 64 * 1024


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
