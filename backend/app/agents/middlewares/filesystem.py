"""工位 2 filesystem：Workdir 与大结果卸载（无沙盒与命令执行）。

超过阈值的大工具结果写入 Workdir 的 outputs/large_tool_results/，请求与持久化里
只保留路径 + 哈希 + 预览，供确定性压缩与人工核查引用；文件按内容哈希命名，
重复卸载同一结果不增长文件。写盘失败不中断 Run，保留原文并告警。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage, ToolMessage

from app.agents.middlewares.base import ModelCallRequest, Station, ToolCallRequest, ToolResult

logger = logging.getLogger(__name__)

LARGE_RESULT_DIR = Path("outputs") / "large_tool_results"
OFFLOAD_MARKER = "[大结果已卸载]"


@dataclass(frozen=True)
class FilesystemSettings:
    enabled: bool = True
    offload_threshold_bytes: int = 8192
    preview_chars: int = 200


class FilesystemStation(Station):
    key = "filesystem"
    label = "filesystem 工作目录"

    def __init__(
        self, *, settings: FilesystemSettings | None = None, workdir: Path | None = None
    ) -> None:
        resolved = settings or FilesystemSettings()
        super().__init__(enabled=resolved.enabled)
        self._settings = resolved
        self._workdir = workdir

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        request.messages = [self._offload_message(message) for message in request.messages]
        return request

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        replacement = self._offload(content_text(result.content), source=call.name)
        if replacement is not None:
            result.content = replacement
        return result

    def _offload_message(self, message: BaseMessage) -> BaseMessage:
        if not isinstance(message, ToolMessage):
            return message
        replacement = self._offload(
            content_text(message.content), source=message.name or str(message.tool_call_id)
        )
        if replacement is None:
            return message
        return message.model_copy(update={"content": replacement})

    def _offload(self, text: str, *, source: str) -> str | None:
        if not text:
            return None
        raw = text.encode("utf-8")
        if len(raw) <= self._settings.offload_threshold_bytes:
            return None
        if self._workdir is None:
            logger.warning("未配置工作目录，大结果保留原文：source=%s bytes=%d", source, len(raw))
            return None
        digest = hashlib.sha256(raw).hexdigest()
        directory = self._workdir / LARGE_RESULT_DIR
        try:
            directory.mkdir(parents=True, exist_ok=True)
            suffix = ".json" if _is_json(text) else ".txt"
            path = directory / f"{digest[:16]}{suffix}"
            if not path.exists():
                path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("大结果卸载失败，保留原文：source=%s error=%s", source, exc)
            return None
        preview = " ".join(text.split())[: self._settings.preview_chars]
        return (
            f"{OFFLOAD_MARKER} source={source} path={path} "
            f"sha256={digest} bytes={len(raw)} preview={preview}"
        )


def content_text(content: Any) -> str:
    """工具结果内容归一为文本：字符串原样，内容块拼接，结构化数据转 JSON。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        ]
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
    except (TypeError, ValueError):
        return False
    return True
