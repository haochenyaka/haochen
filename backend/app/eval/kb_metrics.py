"""知识库评估指标：逐题命中 → 宏观平均，口径显式定义，便于跨版本比较。

口径：
- 黄金参考是文档级（题目给出 gold_titles），检索结果按重排后的段落序去重成文档序列；
- recall@K = 命中的黄金文档数 ÷ 黄金文档数；
- precision@K = 命中的黄金文档数 ÷ min(K, 实际返回文档数)，返回少于 K 时不为空位扣分；
- f1@K = 逐题 P/R 的调和平均（P=R=0 记 0），随后宏观平均；
- primary@1 = 重排首位文档是否为该题主文档（衡量排序质量，黄金集较宽时仍有区分度）；
- chunk_recall@K / chunk_precision@K / chunk_f1@K = 同口径的分块级版本，锚点为
  (document_title, ordinal)，需题目给出 gold_chunks 且检索明细可解析，否则记 None；
- answer_correctness 由评判模型给出（0–1），未配置模型时为 None；
- overall_score 取有答案评判时的 answer_correctness，否则 recall@K（主指标切换见 P2.4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.eval.kb_dataset import EvalItem


@dataclass(frozen=True)
class QuestionOutcome:
    """逐题结果：保留检索明细，供人工复核与分块级标注核对。"""

    item_id: str
    question: str
    gold_titles: tuple[str, ...]
    primary_title: str
    retrieved_titles: tuple[str, ...]
    retrieved: tuple[dict[str, object], ...]
    recall: float
    precision: float
    f1: float
    primary_top1: bool
    degraded: bool
    gold_chunks: tuple[dict[str, object], ...] = ()
    chunk_recall: float | None = None
    chunk_precision: float | None = None
    chunk_f1: float | None = None
    answer: str | None = None
    answer_correctness: float | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "item_id": self.item_id,
            "question": self.question,
            "gold_titles": list(self.gold_titles),
            "primary_title": self.primary_title,
            "retrieved_titles": list(self.retrieved_titles),
            "retrieved": [dict(chunk) for chunk in self.retrieved],
            "recall": round(self.recall, 6),
            "precision": round(self.precision, 6),
            "f1": round(self.f1, 6),
            "primary_top1": self.primary_top1,
            "degraded": self.degraded,
        }
        if self.gold_chunks:
            payload["gold_chunks"] = [dict(chunk) for chunk in self.gold_chunks]
        if self.chunk_recall is not None:
            payload["chunk_recall"] = round(self.chunk_recall, 6)
            payload["chunk_precision"] = round(self.chunk_precision or 0.0, 6)
            payload["chunk_f1"] = round(self.chunk_f1 or 0.0, 6)
        if self.answer is not None:
            payload["answer"] = self.answer
        if self.answer_correctness is not None:
            payload["answer_correctness"] = round(self.answer_correctness, 6)
        return payload


@dataclass(frozen=True)
class EvalMetrics:
    questions: int
    k: int
    recall_at_k: float
    precision_at_k: float
    f1_at_k: float
    primary_top1_rate: float
    hit_rate: float
    answer_correctness: float | None
    answered: int
    overall_score: float
    overall_metric: str
    chunk_recall_at_k: float | None = None
    chunk_precision_at_k: float | None = None
    chunk_f1_at_k: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "questions": self.questions,
            "k": self.k,
            "recall_at_k": round(self.recall_at_k, 6),
            "precision_at_k": round(self.precision_at_k, 6),
            "f1_at_k": round(self.f1_at_k, 6),
            "primary_top1_rate": round(self.primary_top1_rate, 6),
            "hit_rate": round(self.hit_rate, 6),
            "answer_correctness": (
                None if self.answer_correctness is None else round(self.answer_correctness, 6)
            ),
            "answered": self.answered,
            "overall_score": round(self.overall_score, 6),
            "overall_metric": self.overall_metric,
            "chunk_recall_at_k": _round_or_none(self.chunk_recall_at_k),
            "chunk_precision_at_k": _round_or_none(self.chunk_precision_at_k),
            "chunk_f1_at_k": _round_or_none(self.chunk_f1_at_k),
        }


def score_question(
    item: EvalItem,
    retrieved_titles: list[str],
    *,
    k: int,
    retrieved: list[dict[str, object]] | None = None,
    degraded: bool = False,
) -> QuestionOutcome:
    """按题打分：文档级去重序列（保持重排先后）与黄金文档集合求交。

    给出 gold_chunks 且检索明细可解析时，同时计算分块级指标（锚点 (title, ordinal)）。
    """
    ranked = _dedupe(retrieved_titles)[:k]
    gold = set(item.gold_titles)
    hits = len(gold & set(ranked))
    recall = hits / len(gold) if gold else 0.0
    denominator = max(1, min(k, len(ranked)))
    precision = hits / denominator if ranked else 0.0
    f1 = 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)
    chunk_recall, chunk_precision, chunk_f1 = _chunk_metrics(item, retrieved or [], k=k)
    return QuestionOutcome(
        item_id=item.id,
        question=item.question,
        gold_titles=item.gold_titles,
        primary_title=item.primary_title,
        retrieved_titles=tuple(ranked),
        retrieved=tuple(retrieved or ()),
        recall=recall,
        precision=precision,
        f1=f1,
        primary_top1=bool(ranked) and ranked[0] == item.primary_title,
        degraded=degraded,
        gold_chunks=tuple(chunk.as_dict() for chunk in item.gold_chunks),
        chunk_recall=chunk_recall,
        chunk_precision=chunk_precision,
        chunk_f1=chunk_f1,
    )


def _chunk_metrics(
    item: EvalItem, retrieved: list[dict[str, object]], *, k: int
) -> tuple[float | None, float | None, float | None]:
    """分块级打分：仅当题目给出 gold_chunks 时产出，否则三项均记 None。"""
    if not item.gold_chunks:
        return None, None, None
    ranked = _dedupe_chunks(retrieved)[:k]
    gold = {(ref.document_title, ref.ordinal) for ref in item.gold_chunks}
    hits = len(gold & set(ranked))
    recall = hits / len(gold)
    denominator = max(1, min(k, len(ranked)))
    precision = hits / denominator if ranked else 0.0
    f1 = 0.0 if recall + precision == 0 else 2 * recall * precision / (recall + precision)
    return recall, precision, f1


def aggregate(outcomes: list[QuestionOutcome], *, k: int) -> EvalMetrics:
    """宏观平均：每题等权，未评判/未标分块的结果不计入对应分母。"""
    total = len(outcomes)
    if total == 0:
        raise ValueError("评估结果为空，无法聚合")
    scored = [item.answer_correctness for item in outcomes if item.answer_correctness is not None]
    answer_correctness = sum(scored) / len(scored) if scored else None
    recall = _mean([item.recall for item in outcomes])
    overall_score = answer_correctness if answer_correctness is not None else recall
    return EvalMetrics(
        questions=total,
        k=k,
        recall_at_k=recall,
        precision_at_k=_mean([item.precision for item in outcomes]),
        f1_at_k=_mean([item.f1 for item in outcomes]),
        primary_top1_rate=_mean([1.0 if item.primary_top1 else 0.0 for item in outcomes]),
        hit_rate=_mean([1.0 if item.recall > 0 else 0.0 for item in outcomes]),
        answer_correctness=answer_correctness,
        answered=len(scored),
        overall_score=overall_score,
        overall_metric="answer_correctness" if answer_correctness is not None else f"recall@{k}",
        chunk_recall_at_k=_mean_or_none([item.chunk_recall for item in outcomes]),
        chunk_precision_at_k=_mean_or_none([item.chunk_precision for item in outcomes]),
        chunk_f1_at_k=_mean_or_none([item.chunk_f1 for item in outcomes]),
    )


def chunk_digest(chunk: Any) -> dict[str, object]:
    """检索片段 → 报告明细：引用字段与 model_audit.retrieval_refs 同口径。"""
    return {
        "chunk_id": str(chunk.chunk_id),
        "document_id": str(chunk.document_id),
        "document_title": chunk.document_title,
        "ordinal": chunk.ordinal,
        "score": round(float(chunk.score), 6),
        "recall_score": round(float(chunk.recall_score), 6),
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def _dedupe_chunks(retrieved: list[dict[str, object]]) -> list[tuple[str, int]]:
    """检索片段 → 分块锚点序列（保持重排先后，按 (title, ordinal) 去重）。"""
    seen: set[tuple[str, int]] = set()
    ordered: list[tuple[str, int]] = []
    for chunk in retrieved:
        title = chunk.get("document_title")
        ordinal = chunk.get("ordinal")
        if not isinstance(title, str) or not isinstance(ordinal, int) or isinstance(ordinal, bool):
            continue
        anchor = (title, ordinal)
        if anchor in seen:
            continue
        seen.add(anchor)
        ordered.append(anchor)
    return ordered


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _mean_or_none(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(value, 6)
