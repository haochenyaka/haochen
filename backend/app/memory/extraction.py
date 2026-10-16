"""长期记忆候选抽取：LLM 结构化输出为主，规则校验统一在此收口。

抽取是"候选"生成，落库前一律经 `validate_candidates` 过一遍规则（分类白名单、长度、
敏感明文、既有去重、单 Run 上限）。未配置模型密钥或抽取关闭时用空实现，任务不产生候选
也不失败——记忆写入是尽力而为的增强，不阻断 Run 收敛。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.agents.middlewares.credential_guard import scan_sensitive_plaintext
from app.agents.models import build_chat_model
from app.config import get_settings

logger = logging.getLogger(__name__)

ALLOWED_CATEGORIES: tuple[str, ...] = ("preference", "habit", "profile", "note")
"""候选记忆允许的分类：管理端分类非枚举，抽取侧用白名单约束防漂移。"""

EXTRACTION_PROMPT = (
    "你是无人酒店的长期记忆抽取器。从下面的对话中抽取值得长期保留的客人信息，"
    "供下次接待更贴心。只抽稳定、可复用的信息（偏好、习惯、画像）；"
    "不抽一次性事务细节（订单号、金额、日期、当日诉求）、不抽联系方式与证件号码；"
    f"分类只能取：{'、'.join(ALLOWED_CATEGORIES)}。"
    "每条记忆用一句话客观陈述客人的事实，不写建议或承诺；无可抽取内容时返回空列表。"
)


@dataclass(frozen=True)
class MemoryCandidate:
    category: str
    content: str


class MemoryExtractor(Protocol):
    async def extract(self, *, transcript: str) -> Sequence[MemoryCandidate]: ...


class _ExtractedItem(BaseModel):
    category: str = Field(description="记忆分类，只能取提示词给定的枚举值")
    content: str = Field(description="一句话客观陈述客人的事实")


class _ExtractedMemories(BaseModel):
    items: list[_ExtractedItem] = Field(default_factory=list, description="抽取到的候选记忆")


class NullMemoryExtractor:
    """未配置模型或抽取关闭时的空实现：不产生候选，不阻断任务。"""

    async def extract(self, *, transcript: str) -> Sequence[MemoryCandidate]:
        return ()


class LlmMemoryExtractor:
    """结构化输出抽取器：模型不可用或返回异常时返回空（不抛出）。"""

    def __init__(self, model: BaseChatModel) -> None:
        self._structured = model.with_structured_output(_ExtractedMemories)

    async def extract(self, *, transcript: str) -> Sequence[MemoryCandidate]:
        try:
            result = await self._structured.ainvoke(
                [SystemMessage(content=EXTRACTION_PROMPT), HumanMessage(content=transcript)]
            )
        except Exception as exc:  # 抽取失败不阻断任务：记日志后按无候选处理
            logger.warning("记忆抽取模型调用失败，跳过：%s", exc)
            return ()
        if isinstance(result, _ExtractedMemories):
            return tuple(MemoryCandidate(item.category, item.content) for item in result.items)
        return ()


def build_memory_extractor() -> MemoryExtractor:
    """按设置构建抽取器：关闭、无密钥或构建失败时落空实现。"""
    settings = get_settings()
    if not settings.memory_extract_enabled or not settings.model_api_key:
        return NullMemoryExtractor()
    try:
        model = build_chat_model(config={"temperature": 0.0})
    except Exception as exc:
        logger.warning("记忆抽取模型构建失败，跳过：%s", exc)
        return NullMemoryExtractor()
    if not isinstance(model, ChatOpenAI):
        return NullMemoryExtractor()
    return LlmMemoryExtractor(model)


def validate_candidates(
    candidates: Sequence[MemoryCandidate], *, existing: set[tuple[str, str]]
) -> list[MemoryCandidate]:
    """规则校验：分类白名单、长度区间、敏感明文、与既有/本批去重、单 Run 上限。

    `existing` 为既有记忆的 (category, content) 键集（含软删），命中即跳过——不让被
    删除的记忆被再次抽取"复活"，也让任务重跑保持幂等。
    """
    settings = get_settings()
    min_chars = settings.memory_extract_min_chars
    max_chars = settings.memory_extract_max_chars
    kept: list[MemoryCandidate] = []
    seen = set(existing)
    for candidate in candidates:
        category = candidate.category.strip()
        content = candidate.content.strip()
        if category not in ALLOWED_CATEGORIES:
            continue
        if not (min_chars <= len(content) <= max_chars):
            continue
        if scan_sensitive_plaintext(content):
            continue
        key = (category, content)
        if key in seen:
            continue
        seen.add(key)
        kept.append(MemoryCandidate(category, content))
        if len(kept) >= settings.memory_extract_max_items:
            break
    return kept


__all__ = [
    "ALLOWED_CATEGORIES",
    "EXTRACTION_PROMPT",
    "LlmMemoryExtractor",
    "MemoryCandidate",
    "MemoryExtractor",
    "NullMemoryExtractor",
    "build_memory_extractor",
    "validate_candidates",
]
