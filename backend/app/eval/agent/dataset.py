"""智能体评估数据集：YAML 定义测试用例，含输入、预期工具链与任务结果。

每条用例可指定前置对话历史、客人属性覆盖、预期工具调用序列与任务成功标志。
数据集指纹用于基线可比性。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class EvalSetError(ValueError):
    """评估集格式不合法：字段缺失、id 重复或预期定义不完整。"""


@dataclass
class ToolCallSpec:
    """预期工具调用规范：按名称匹配，可选检查参数子集。"""

    name: str
    args_contain: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        d: dict[str, object] = {"name": self.name}
        if self.args_contain is not None:
            d["args_contain"] = dict(self.args_contain)
        return d


@dataclass
class ExpectedSpec:
    """用例预期结果：工具序列、任务成功标志与回答关键词。"""

    tool_sequence: list[ToolCallSpec] | None = None
    task_success: bool | None = None
    answer_contains: list[str] | None = None

    def as_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "tool_sequence": [t.as_dict() for t in (self.tool_sequence or [])],
        }
        if self.task_success is not None:
            d["task_success"] = self.task_success
        if self.answer_contains:
            d["answer_contains"] = list(self.answer_contains)
        return d


@dataclass
class HistoryMessage:
    """前置对话消息。"""

    role: str
    content: str


@dataclass(frozen=True)
class AgentEvalItem:
    id: str
    name: str
    message: str
    history: tuple[HistoryMessage, ...] = ()
    guest_overrides: dict[str, object] | None = None
    expected: ExpectedSpec | None = None
    tags: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "id": self.id,
            "name": self.name,
            "message": self.message,
            "history": [{"role": m.role, "content": m.content} for m in self.history],
            "expected": (self.expected or ExpectedSpec()).as_dict(),
            "tags": list(self.tags),
        }
        if self.guest_overrides is not None:
            d["guest_overrides"] = dict(self.guest_overrides)
        return d


def load_eval_set(path: Path) -> list[AgentEvalItem]:
    """读 YAML 评估集：校验字段、id 唯一。"""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "cases" not in raw:
        raise EvalSetError(f"{path} 必须包含 cases 顶层键")
    cases = raw["cases"]
    if not isinstance(cases, list) or not cases:
        raise EvalSetError(f"{path} cases 必须为非空数组")
    items: list[AgentEvalItem] = []
    seen: set[str] = set()
    for idx, case in enumerate(cases):
        item = _parse_item(path, idx, case)
        if item.id in seen:
            raise EvalSetError(f"{path}: case #{idx} id 重复：{item.id}")
        seen.add(item.id)
        items.append(item)
    return items


def fingerprint(payload: object) -> str:
    """通用指纹：规范化 JSON 后取 sha256。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dataset_fingerprint(items: Sequence[AgentEvalItem]) -> str:
    """数据集指纹：任一条目变化即失配。"""
    return fingerprint([item.as_dict() for item in items])


def _parse_item(path: Path, idx: int, case: object) -> AgentEvalItem:
    if not isinstance(case, dict):
        raise EvalSetError(f"{path}: case #{idx} 必须是对象")
    case_id = case.get("id", "")
    name = case.get("name", "")
    message = case.get("message", "")
    if not isinstance(case_id, str) or not case_id.strip():
        raise EvalSetError(f"{path}: case #{idx} id 缺失或非空字符串")
    if not isinstance(message, str) or not message.strip():
        raise EvalSetError(f"{path}: case #{idx} message 缺失或非空字符串")
    # parse history
    history: tuple[HistoryMessage, ...] = ()
    raw_history = case.get("history")
    if raw_history is not None:
        if not isinstance(raw_history, list):
            raise EvalSetError(f"{path}: case #{idx} history 必须为数组")
        parsed: list[HistoryMessage] = []
        for h_idx, h in enumerate(raw_history):
            if not isinstance(h, dict) or "role" not in h or "content" not in h:
                raise EvalSetError(f"{path}: case #{idx} history[{h_idx}] 缺少 role/content")
            parsed.append(HistoryMessage(role=str(h["role"]), content=str(h["content"])))
        history = tuple(parsed)
    # parse guest_overrides
    guest_overrides = case.get("guest_overrides")
    if guest_overrides is not None and not isinstance(guest_overrides, dict):
        raise EvalSetError(f"{path}: case #{idx} guest_overrides 必须为对象或 null")
    # parse expected
    raw_expected = case.get("expected")
    expected: ExpectedSpec | None = None
    if raw_expected is not None:
        if not isinstance(raw_expected, dict):
            raise EvalSetError(f"{path}: case #{idx} expected 必须为对象")
        expected = _parse_expected(raw_expected)
    # parse tags
    tags: tuple[str, ...] = ()
    raw_tags = case.get("tags")
    if raw_tags is not None:
        if not isinstance(raw_tags, list) or not all(isinstance(t, str) for t in raw_tags):
            raise EvalSetError(f"{path}: case #{idx} tags 必须为字符串数组")
        tags = tuple(raw_tags)
    return AgentEvalItem(
        id=str(case_id).strip(),
        name=str(name).strip() if name else str(case_id).strip(),
        message=message.strip(),
        history=history,
        guest_overrides=guest_overrides,
        expected=expected,
        tags=tags,
    )


def _parse_expected(raw: dict[str, Any]) -> ExpectedSpec:
    tool_sequence: list[ToolCallSpec] = []
    raw_seq = raw.get("tool_sequence")
    if raw_seq is not None:
        if not isinstance(raw_seq, list):
            raise EvalSetError("tool_sequence 必须为数组")
        for t_idx, t in enumerate(raw_seq):
            if not isinstance(t, dict) or "name" not in t:
                raise EvalSetError(f"tool_sequence[{t_idx}] 缺少 name")
            tool_sequence.append(
                ToolCallSpec(
                    name=str(t["name"]),
                    args_contain=dict(t.get("args_contain", {})) if t.get("args_contain") else None,
                )
            )
    return ExpectedSpec(
        tool_sequence=tool_sequence,
        task_success=raw.get("task_success"),
        answer_contains=(
            list(raw["answer_contains"]) if isinstance(raw.get("answer_contains"), list) else None
        ),
    )
