"""文档解析与分块：把原件转成可检索正文，并切分为带偏移的分块。

不变量：
- 解析失败一律抛出 `ParseError`（携带可读原因），由任务层收敛为 error_parsing；
- 分块正文 = 原件文本的连续切片，`char_start/char_end` 指向原文档，引用可回溯；
- 先按 markdown 标题切小节、再在小节内按段落边界聚合，超长段落按句读再切；重叠区只在
  小节内保留——跨小节重叠会把两个主题混进同一分块，破坏分块级黄金标注的语义。
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from pypdf import PdfReader

SUPPORTED_SOURCE_TYPES: frozenset[str] = frozenset({"markdown", "text", "pdf"})

_EXTENSION_SOURCE_TYPES: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".pdf": "pdf",
}

_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n+")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;.\n])")
_HEADING_START = re.compile(r"(?m)^(?=#{1,6}\s)")
_LEADING_HEADING = re.compile(r"^#{1,6}\s+.*?(?:\n|$)")


class ParseError(Exception):
    """原件不可解析：编码、格式或内容为空。"""


@dataclass(frozen=True)
class ChunkDraft:
    ordinal: int
    content: str
    content_hash: str
    char_start: int
    char_end: int


def source_type_for_filename(filename: str) -> str | None:
    """按扩展名判定文档类型；未知扩展名返回 None（调用方拒绝上传）。"""
    suffix = PurePosixPath(filename).suffix.lower()
    return _EXTENSION_SOURCE_TYPES.get(suffix)


def extract_text(*, source_type: str, data: bytes) -> str:
    if source_type not in SUPPORTED_SOURCE_TYPES:
        raise ParseError(f"不支持的文档类型：{source_type}")
    if source_type == "pdf":
        text = _extract_pdf(data)
    else:
        text = _decode_text(data, source_type=source_type)
    normalized = _normalize(text)
    if not normalized.strip():
        raise ParseError("解析结果为空：原件没有可用正文")
    return normalized


def _decode_text(data: bytes, *, source_type: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ParseError(f"{source_type} 文件不是可识别的文本编码（尝试 utf-8 / gb18030）")


def _extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except ParseError:
        raise
    except Exception as exc:
        raise ParseError(f"PDF 解析失败：{exc}") from exc
    return "\n\n".join(pages)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(
    text: str,
    *,
    max_chars: int = 700,
    overlap_chars: int = 80,
) -> list[ChunkDraft]:
    """按 markdown 标题分小节，小节内按段落聚合分块；超长段落按句读切分。

    `char_start/char_end` 指向解析产物（而非原件字节），引用可按偏移回溯到原文；
    每个分块的长度（去首尾空白后）不超过 `max_chars`。
    """
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars 必须落在 [0, max_chars)")

    drafts: list[ChunkDraft] = []
    for start, end in _section_ranges(text):
        _chunk_range(
            text,
            start=start,
            end=end,
            max_chars=max_chars,
            overlap_chars=overlap_chars,
            drafts=drafts,
        )
    if not drafts:
        raise ParseError("解析结果无法分块：正文为空")
    return drafts


def _section_ranges(text: str) -> list[tuple[int, int]]:
    """按 markdown 标题行切分整篇文本，返回 [start, end) 区间。

    「只有标题行、没有正文」的小节并入下一节，避免产生纯标题分块（如 `# 标题` 单独一行）。
    """
    starts = [match.start() for match in _HEADING_START.finditer(text)]
    if not starts:
        return [(0, len(text))]
    bounds = ([0] if starts[0] > 0 else []) + starts + [len(text)]
    sections = [(bounds[index], bounds[index + 1]) for index in range(len(bounds) - 1)]
    merged: list[tuple[int, int]] = []
    for start, end in sections:
        if (
            merged
            and not _LEADING_HEADING.sub("", text[merged[-1][0] : merged[-1][1]], count=1).strip()
        ):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _chunk_range(
    text: str,
    *,
    start: int,
    end: int,
    max_chars: int,
    overlap_chars: int,
    drafts: list[ChunkDraft],
) -> None:
    """在 [start, end) 区间内聚合分块，偏移量换算回整篇文本的绝对下标。"""
    pieces = [
        (piece, start + offset)
        for piece, offset in _split_to_pieces(text[start:end], max_chars=max_chars)
    ]
    current: list[tuple[str, int]] = []

    def _append(pieces_in_chunk: list[tuple[str, int]]) -> None:
        chunk_start = pieces_in_chunk[0][1]
        chunk_end = pieces_in_chunk[-1][1] + len(pieces_in_chunk[-1][0])
        raw = text[chunk_start:chunk_end]
        content = raw.strip()
        if not content:
            return
        lead = len(raw) - len(raw.lstrip())
        drafts.append(
            ChunkDraft(
                ordinal=len(drafts),
                content=content,
                content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                char_start=chunk_start + lead,
                char_end=chunk_start + lead + len(content),
            )
        )

    for piece, offset in pieces:
        if current and offset + len(piece) - current[0][1] > max_chars:
            _append(current)
            tail = _overlap_tail(current, overlap_chars=overlap_chars)
            current = tail if tail and offset + len(piece) - tail[0][1] <= max_chars else []
        current.append((piece, offset))

    if current:
        _append(current)


def _overlap_tail(pieces: list[tuple[str, int]], *, overlap_chars: int) -> list[tuple[str, int]]:
    """取尾部若干片段作为下一分块的重叠区，总量达到 overlap_chars 即停。"""
    tail: list[tuple[str, int]] = []
    tail_len = 0
    for item in reversed(pieces):
        if tail_len >= overlap_chars:
            break
        tail.insert(0, item)
        tail_len += len(item[0])
    return tail


def _split_to_pieces(text: str, *, max_chars: int) -> list[tuple[str, int]]:
    """产出 (片段, 在解析产物中的起始偏移)，片段长度不超过 max_chars。"""
    pieces: list[tuple[str, int]] = []
    cursor = 0
    for paragraph in _PARAGRAPH_SPLIT.split(text):
        if not paragraph.strip():
            continue
        offset = text.find(paragraph, cursor)
        if offset < 0:
            offset = cursor
        cursor = offset + len(paragraph)
        if len(paragraph) <= max_chars:
            pieces.append((paragraph, offset))
            continue
        sentence_cursor = offset
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            if not sentence.strip():
                continue
            sentence_offset = text.find(sentence, sentence_cursor)
            if sentence_offset < 0:
                sentence_offset = sentence_cursor
            sentence_cursor = sentence_offset + len(sentence)
            if len(sentence) <= max_chars:
                pieces.append((sentence, sentence_offset))
            else:
                pieces.extend(_hard_split(sentence, offset=sentence_offset, max_chars=max_chars))
    return pieces


def _hard_split(text: str, *, offset: int, max_chars: int) -> list[tuple[str, int]]:
    return [
        (text[index : index + max_chars], offset + index)
        for index in range(0, len(text), max_chars)
    ]
