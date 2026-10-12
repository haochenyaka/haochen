"""答案生成与评判：生成侧复用生产回答指令，评判侧让模型对照参考答案打分。

不变量：
- 生成侧系统提示与 `query_kb` 工具下发的回答要求同源，评估口径与线上不漂移；
- 评判模型温度归零，同一答案重复评判可复现；解析失败返回 None，该题不计入
  answer_correctness 分母，不阻塞其余题目；模型调用本身失败直接抛出（评估要响亮地失败）；
- 未配置模型密钥时不构建评判器，评估退化为纯检索指标（answer_correctness 记 null）。
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.models import build_chat_model
from app.agents.toolkits.knowledge import ANSWER_INSTRUCTION
from app.config import Settings, get_settings
from app.eval.kb_dataset import EvalItem

JUDGE_SYSTEM_PROMPT = (
    "你是答案评判器，对照参考答案给候选回答打分。"
    "完全正确且覆盖参考答案要点记 1.0；部分正确记 0.1-0.9；错误、答非所问或凭空编造记 0。"
    '只输出 JSON：{"score": 0.0-1.0}，不要输出解释。'
)
SCORE_FIELD_PATTERN = re.compile(r"score[\"']?\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class AnswerEvaluator(Protocol):
    """评估器契约：生成回答 + 评判正确性，便于测试注入确定性替身。"""

    @property
    def model_name(self) -> str: ...

    async def answer(self, item: EvalItem, context_text: str) -> str: ...

    async def judge(self, item: EvalItem, answer: str) -> float | None: ...


class LlmAnswerEvaluator:
    """同一模型的两次调用：先生成回答，再对照参考答案打分。"""

    def __init__(
        self, answer_model: BaseChatModel, judge_model: BaseChatModel, *, model_name: str
    ) -> None:
        self._answer_model = answer_model
        self._judge_model = judge_model
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    async def answer(self, item: EvalItem, context_text: str) -> str:
        messages = [
            SystemMessage(content=ANSWER_INSTRUCTION),
            HumanMessage(
                content=f"参考资料：\n{context_text}\n\n用户问题：{item.question}",
            ),
        ]
        reply = await self._answer_model.ainvoke(messages)
        return _text(reply.content).strip()

    async def judge(self, item: EvalItem, answer: str) -> float | None:
        payload = {"问题": item.question, "参考答案": item.gold_answer, "候选回答": answer}
        messages = [
            SystemMessage(content=JUDGE_SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]
        reply = await self._judge_model.ainvoke(messages)
        return parse_score(_text(reply.content))


def parse_score(text: str) -> float | None:
    """评判输出 → 0-1 分：优先 JSON 的 score 字段，其次带 score 关键字的数字，最后裸数字。

    0-10 口径的裸数字按十分制折算；越界或解析不出返回 None（该题不进入分母）。
    """
    payload = _load_json_object(text)
    if payload is not None:
        value = payload.get("score")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _normalize(float(value))
    match = SCORE_FIELD_PATTERN.search(text)
    if match is not None:
        return _normalize(float(match.group(1)))
    match = NUMBER_PATTERN.search(text)
    if match is None:
        return None
    return _normalize(float(match.group(0)))


def build_answer_evaluator(settings: Settings | None = None) -> LlmAnswerEvaluator | None:
    """有模型密钥时构建评判器；开发态无密钥返回 None，评估只出检索指标。"""
    settings = settings or get_settings()
    if not settings.model_api_key or not settings.model_name:
        return None
    return LlmAnswerEvaluator(
        build_chat_model(),
        build_chat_model(config={"temperature": 0}),
        model_name=settings.model_name,
    )


def _normalize(value: float) -> float | None:
    if 0.0 <= value <= 1.0:
        return value
    if 1.0 < value <= 10.0:
        return round(value / 10.0, 6)
    return None


def _load_json_object(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        _, _, stripped = stripped.partition("\n")
    try:
        parsed = json.loads(stripped)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    return str(content)
