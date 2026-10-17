"""评估集装载：JSONL 逐行校验，黄金参考含分块锚点，数据集指纹用于基线可比性。

黄金参考以 (document_title, ordinal) 为分块锚点：chunk_id 随重新索引变化、不可用于标注；
标题在内容不变时稳定，段序在分块配置不变时稳定，因此两者组合作为锚点，并保留标题级
gold_titles/primary_title 作参考口径。锚点的稳定性由内容指纹兜底——内容指纹含逐分块
content_hash（见 kb_runner.collect_content_fingerprint），任一分块内容或边界变化即失配，
避免「改了内容但分块数不变」时锚点静默漂移。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class EvalSetError(ValueError):
    """评估集格式不合法：字段缺失、id 重复或黄金参考为空。"""


@dataclass(frozen=True)
class ChunkRef:
    """黄金分块锚点：文档标题 + 段序（ordinal 从 0 起）。"""

    document_title: str
    ordinal: int

    def as_dict(self) -> dict[str, object]:
        return {"document_title": self.document_title, "ordinal": self.ordinal}


@dataclass(frozen=True)
class EvalItem:
    id: str
    question: str
    kb_code: str
    gold_titles: tuple[str, ...]
    primary_title: str
    gold_answer: str
    gold_chunks: tuple[ChunkRef, ...] = ()
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "question": self.question,
            "kb_code": self.kb_code,
            "gold_titles": list(self.gold_titles),
            "primary_title": self.primary_title,
            "gold_answer": self.gold_answer,
            "gold_chunks": [chunk.as_dict() for chunk in self.gold_chunks],
            "tags": list(self.tags),
        }


def load_eval_set(path: Path) -> list[EvalItem]:
    """读 JSONL 评估集：空行跳过，逐行校验字段，id 全局唯一。"""
    items: list[EvalItem] = []
    seen: set[str] = set()
    text = path.read_text(encoding="utf-8")
    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalSetError(f"{path}:{line_no} 不是合法 JSON：{exc}") from exc
        item = _parse_item(path, line_no, payload)
        if item.id in seen:
            raise EvalSetError(f"{path}:{line_no} id 重复：{item.id}")
        seen.add(item.id)
        items.append(item)
    if not items:
        raise EvalSetError(f"{path} 评估集为空")
    return items


def fingerprint(payload: object) -> str:
    """通用指纹：规范化 JSON（键排序、中文不转义）后取 sha256，跨报告可比较。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_fingerprint(items: Sequence[EvalItem]) -> str:
    """数据集指纹：任一条目（含黄金答案）变化即失配，旧基线不可直接比较。"""
    return fingerprint([item.as_dict() for item in items])


def _parse_item(path: Path, line_no: int, payload: object) -> EvalItem:
    if not isinstance(payload, dict):
        raise EvalSetError(f"{path}:{line_no} 条目必须是 JSON 对象")
    gold_titles = _str_tuple(path, line_no, payload, "gold_titles", required=True)
    gold_chunks = _chunk_refs(path, line_no, payload, gold_titles)
    item = EvalItem(
        id=_str_field(path, line_no, payload, "id"),
        question=_str_field(path, line_no, payload, "question"),
        kb_code=_str_field(path, line_no, payload, "kb_code"),
        gold_titles=gold_titles,
        primary_title=_str_field(path, line_no, payload, "primary_title"),
        gold_answer=_str_field(path, line_no, payload, "gold_answer"),
        gold_chunks=gold_chunks,
        tags=_str_tuple(path, line_no, payload, "tags", required=False),
    )
    if item.primary_title not in item.gold_titles:
        raise EvalSetError(f"{path}:{line_no} primary_title 不在 gold_titles 内：{item.id}")
    return item


def _chunk_refs(
    path: Path, line_no: int, payload: dict[str, object], gold_titles: tuple[str, ...]
) -> tuple[ChunkRef, ...]:
    """解析分块锚点；缺省允许为空（分块级标注为可选，出厂集由守卫测试强制非空）。"""
    value = payload.get("gold_chunks")
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise EvalSetError(f"{path}:{line_no} 缺少非空数组字段：gold_chunks")
    refs: list[ChunkRef] = []
    for idx, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise EvalSetError(f"{path}:{line_no} gold_chunks[{idx}] 必须是对象")
        title = entry.get("document_title")
        ordinal = entry.get("ordinal")
        if not isinstance(title, str) or not title.strip():
            raise EvalSetError(f"{path}:{line_no} gold_chunks[{idx}] 缺少 document_title")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
            raise EvalSetError(f"{path}:{line_no} gold_chunks[{idx}] ordinal 必须是非负整数")
        if title.strip() not in gold_titles:
            raise EvalSetError(
                f"{path}:{line_no} gold_chunks[{idx}] 标题不在 gold_titles 内：{title.strip()}"
            )
        refs.append(ChunkRef(document_title=title.strip(), ordinal=ordinal))
    return tuple(refs)


def _str_field(path: Path, line_no: int, payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise EvalSetError(f"{path}:{line_no} 缺少非空字符串字段：{key}")
    return value.strip()


def _str_tuple(
    path: Path, line_no: int, payload: dict[str, object], key: str, *, required: bool
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value:
        raise EvalSetError(f"{path}:{line_no} 缺少非空数组字段：{key}")
    if not all(isinstance(entry, str) and entry.strip() for entry in value):
        raise EvalSetError(f"{path}:{line_no} 字段 {key} 必须是字符串数组")
    return tuple(str(entry).strip() for entry in value)
