"""评估集装载：JSONL 逐行校验，黄金参考以文档标题为准，数据集指纹用于基线可比性。

黄金参考为什么不用 chunk_id：分块会随分块配置变化，标题在内容不变时稳定；分块级变化由
内容指纹（文档 checksum + 分块数）兜底，per-question 结果保留 chunk_id 与段序，后续可
升级为分块级黄金标注。
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
class EvalItem:
    id: str
    question: str
    kb_code: str
    gold_titles: tuple[str, ...]
    primary_title: str
    gold_answer: str
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "question": self.question,
            "kb_code": self.kb_code,
            "gold_titles": list(self.gold_titles),
            "primary_title": self.primary_title,
            "gold_answer": self.gold_answer,
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
    item = EvalItem(
        id=_str_field(path, line_no, payload, "id"),
        question=_str_field(path, line_no, payload, "question"),
        kb_code=_str_field(path, line_no, payload, "kb_code"),
        gold_titles=gold_titles,
        primary_title=_str_field(path, line_no, payload, "primary_title"),
        gold_answer=_str_field(path, line_no, payload, "gold_answer"),
        tags=_str_tuple(path, line_no, payload, "tags", required=False),
    )
    if item.primary_title not in item.gold_titles:
        raise EvalSetError(f"{path}:{line_no} primary_title 不在 gold_titles 内：{item.id}")
    return item


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
