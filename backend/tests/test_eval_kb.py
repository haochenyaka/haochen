"""知识库评估单元测试：评估集校验、指标口径、评判解析与出厂评估集漂移守卫。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.config import get_settings
from app.eval.kb_dataset import (
    ChunkRef,
    EvalItem,
    EvalSetError,
    dataset_fingerprint,
    fingerprint,
    load_eval_set,
)
from app.eval.kb_judge import parse_score
from app.eval.kb_metrics import aggregate, chunk_digest, score_question
from app.eval.kb_runner import (
    DEFAULT_SET_PATH,
    EvalRunError,
    evaluate_items,
    retrieval_config_snapshot,
)
from app.knowledge.retrieval import RetrievedChunk, RetrievedContext
from app.knowledge.seed import KB_CODE, SAMPLE_README, SAMPLE_TITLES, SAMPLES_DIR


def _item_payload(item_id: str, *, question: str = "早餐几点开始？") -> dict[str, Any]:
    return {
        "id": item_id,
        "question": question,
        "kb_code": KB_CODE,
        "gold_titles": ["【样例】早餐与餐饮"],
        "primary_title": "【样例】早餐与餐饮",
        "gold_answer": "每天 7:00-9:30。",
        "tags": ["早餐"],
    }


def _write_set(tmp_path: Path, name: str, payloads: list[dict[str, Any]]) -> Path:
    path = tmp_path / name
    path.write_text(
        "\n".join(json.dumps(payload, ensure_ascii=False) for payload in payloads) + "\n",
        encoding="utf-8",
    )
    return path


def _item(item_id: str, question: str, *, gold: tuple[str, ...], primary: str) -> EvalItem:
    return EvalItem(
        id=item_id,
        question=question,
        kb_code="hotel-guest",
        gold_titles=gold,
        primary_title=primary,
        gold_answer="参考答案",
        tags=("单元",),
    )


def _chunk(position: int, title: str) -> RetrievedChunk:
    return RetrievedChunk(
        index=position + 1,
        chunk_id=uuid4(),
        kb_id=uuid4(),
        document_id=uuid4(),
        document_title=title,
        ordinal=position,
        content=f"《{title}》第 {position} 段正文",
        score=round(1.0 - position * 0.1, 6),
        recall_score=round(1.0 - position * 0.1, 6),
        char_start=0,
        char_end=12,
    )


class FakeRetriever:
    """按问题返回固定文档序列的检索替身：只关心「题目 → 文档序」映射与降级标记。"""

    def __init__(self, ranking: dict[str, list[str]], *, degraded: bool = False) -> None:
        self._ranking = ranking
        self._degraded = degraded
        self.calls: list[tuple[str, int | None]] = []

    async def retrieve(
        self, query: str, *, kb_ids: Any = None, top_n: int | None = None
    ) -> RetrievedContext:
        self.calls.append((query, top_n))
        titles = self._ranking.get(query, [])
        limited = titles[: top_n or len(titles)]
        chunks = tuple(_chunk(position, title) for position, title in enumerate(limited))
        prompt_text = "\n\n".join(
            f"[{position + 1}]《{title}》" for position, title in enumerate(limited)
        )
        return RetrievedContext(
            query=query, chunks=chunks, prompt_text=prompt_text, degraded=self._degraded
        )


class FakeEvaluator:
    model_name = "fake-judge"

    def __init__(self, scores: dict[str, float | None]) -> None:
        self._scores = scores
        self.answers: list[str] = []

    async def answer(self, item: EvalItem, context_text: str) -> str:
        answer = f"答案:{item.id}"
        self.answers.append(answer)
        return answer

    async def judge(self, item: EvalItem, answer: str) -> float | None:
        return self._scores.get(item.id)


def test_load_eval_set_validates_fields_and_rejects_bad_lines(tmp_path: Path) -> None:
    valid = _write_set(tmp_path, "ok.jsonl", [_item_payload("q1"), _item_payload("q2")])
    items = load_eval_set(valid)
    assert [item.id for item in items] == ["q1", "q2"]
    assert items[0].kb_code == KB_CODE

    with pytest.raises(EvalSetError, match="缺少非空数组字段"):
        load_eval_set(_write_set(tmp_path, "missing.jsonl", [{"id": "q1"}]))
    with pytest.raises(EvalSetError, match="缺少非空字符串字段"):
        payload = _item_payload("q1")
        del payload["question"]
        load_eval_set(_write_set(tmp_path, "missing-question.jsonl", [payload]))
    with pytest.raises(EvalSetError, match="id 重复"):
        load_eval_set(_write_set(tmp_path, "dup.jsonl", [_item_payload("q1"), _item_payload("q1")]))
    with pytest.raises(EvalSetError, match="primary_title 不在 gold_titles"):
        payload = _item_payload("q1")
        payload["primary_title"] = "不存在的文档"
        load_eval_set(_write_set(tmp_path, "primary.jsonl", [payload]))
    with pytest.raises(EvalSetError, match="不是合法 JSON"):
        path = tmp_path / "broken.jsonl"
        path.write_text("{不是 JSON}\n", encoding="utf-8")
        load_eval_set(path)
    with pytest.raises(EvalSetError, match="评估集为空"):
        load_eval_set(_write_set(tmp_path, "empty.jsonl", []))


def test_dataset_fingerprint_is_stable_and_content_sensitive(tmp_path: Path) -> None:
    first = _write_set(tmp_path, "first.jsonl", [_item_payload("q1"), _item_payload("q2")])
    items = load_eval_set(first)
    assert dataset_fingerprint(items) == dataset_fingerprint(load_eval_set(first))

    payload = _item_payload("q1")
    payload["gold_answer"] = "改过的参考答案"
    changed = _write_set(tmp_path, "changed.jsonl", [payload])
    assert dataset_fingerprint(load_eval_set(changed)) != dataset_fingerprint(items)
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})


def test_sample_titles_cover_all_sample_documents() -> None:
    """样例目录里的每篇文档都必须有中文标题映射：缺映射会退化成英文文件名进检索引用。"""
    files = {path.name for path in SAMPLES_DIR.glob("*.md")} - {SAMPLE_README}
    assert files == set(SAMPLE_TITLES), files ^ set(SAMPLE_TITLES)


def test_shipped_eval_set_aligns_with_sample_titles_and_carries_anchors() -> None:
    items = load_eval_set(DEFAULT_SET_PATH)
    assert len(items) >= 150
    assert len({item.id for item in items}) == len(items)
    assert {item.kb_code for item in items} == {KB_CODE}
    titles = set(SAMPLE_TITLES.values())
    for item in items:
        assert set(item.gold_titles) <= titles, item.id
        assert item.question.strip() and item.gold_answer.strip()
        assert item.gold_chunks, f"{item.id} 缺分块级黄金锚点"
        assert {ref.document_title for ref in item.gold_chunks} <= set(item.gold_titles), item.id


def test_gold_chunks_parse_and_validate(tmp_path: Path) -> None:
    payload = _item_payload("q1")
    payload["gold_chunks"] = [{"document_title": "【样例】早餐与餐饮", "ordinal": 2}]
    item = load_eval_set(_write_set(tmp_path, "chunks.jsonl", [payload]))[0]
    assert item.gold_chunks == (ChunkRef(document_title="【样例】早餐与餐饮", ordinal=2),)

    detached = _item_payload("q1")
    detached["gold_chunks"] = [{"document_title": "别的文档", "ordinal": 0}]
    with pytest.raises(EvalSetError, match="标题不在 gold_titles"):
        load_eval_set(_write_set(tmp_path, "detached.jsonl", [detached]))

    for bad in (-1, "0", 1.5, True):
        payload = _item_payload("q1")
        payload["gold_chunks"] = [{"document_title": "【样例】早餐与餐饮", "ordinal": bad}]
        with pytest.raises(EvalSetError, match="ordinal 必须是非负整数"):
            load_eval_set(_write_set(tmp_path, f"bad-{bad}.jsonl", [payload]))

    empty = _item_payload("q1")
    empty["gold_chunks"] = []
    with pytest.raises(EvalSetError, match="缺少非空数组字段：gold_chunks"):
        load_eval_set(_write_set(tmp_path, "empty-chunks.jsonl", [empty]))


def test_score_question_chunk_level_metrics() -> None:
    item = replace(
        _item("q1", "早餐几点开始？", gold=("A",), primary="A"),
        gold_chunks=(ChunkRef("A", 0), ChunkRef("A", 2)),
    )
    retrieved = [
        chunk_digest(_chunk(2, "A")),
        chunk_digest(_chunk(0, "B")),
        chunk_digest(_chunk(0, "A")),
    ]
    outcome = score_question(item, ["A", "B"], k=3, retrieved=retrieved)
    assert outcome.chunk_recall == 1.0
    assert outcome.chunk_precision == pytest.approx(2 / 3)
    assert outcome.chunk_f1 == pytest.approx(0.8)
    assert outcome.as_dict()["gold_chunks"] == [
        {"document_title": "A", "ordinal": 0},
        {"document_title": "A", "ordinal": 2},
    ]

    miss = score_question(item, ["B"], k=3, retrieved=[chunk_digest(_chunk(0, "B"))])
    assert miss.chunk_recall == 0.0

    # 未标注分块 → 三项均记 None，且不写入报告明细
    plain = score_question(
        _item("q2", "问题", gold=("A",), primary="A"),
        ["A"],
        k=3,
        retrieved=[chunk_digest(_chunk(0, "A"))],
    )
    assert (plain.chunk_recall, plain.chunk_precision, plain.chunk_f1) == (None, None, None)
    assert "gold_chunks" not in plain.as_dict()


def test_aggregate_includes_chunk_level_means() -> None:
    hit_item = replace(_item("q1", "一", gold=("A",), primary="A"), gold_chunks=(ChunkRef("A", 0),))
    miss_item = replace(
        _item("q2", "二", gold=("A",), primary="A"), gold_chunks=(ChunkRef("A", 0),)
    )
    hit = score_question(hit_item, ["A"], k=3, retrieved=[chunk_digest(_chunk(0, "A"))])
    miss = score_question(miss_item, ["A"], k=3, retrieved=[chunk_digest(_chunk(1, "A"))])
    metrics = aggregate([hit, miss], k=3)
    assert metrics.chunk_recall_at_k == 0.5
    assert metrics.as_dict()["chunk_recall_at_k"] == 0.5

    unmarked = aggregate(
        [score_question(_item("q3", "三", gold=("A",), primary="A"), ["A"], k=3)], k=3
    )
    assert unmarked.chunk_recall_at_k is None
    assert unmarked.as_dict()["chunk_recall_at_k"] is None


def test_score_question_metrics_follow_documented_denominators() -> None:
    item = _item("q1", "早餐几点开始？", gold=("A", "B"), primary="A")
    outcome = score_question(item, ["A", "A", "C"], k=2)
    assert outcome.retrieved_titles == ("A", "C")
    assert outcome.recall == 0.5
    assert outcome.precision == 0.5
    assert outcome.f1 == 0.5
    assert outcome.primary_top1 is True

    short = score_question(item, ["B"], k=10)
    assert short.recall == 0.5
    # 返回少于 K 时不为空位扣分：命中 1 / 实际返回 1
    assert short.precision == 1.0
    assert short.primary_top1 is False

    miss = score_question(item, [], k=10)
    assert (miss.recall, miss.precision, miss.f1, miss.primary_top1) == (0.0, 0.0, 0.0, False)


def test_aggregate_prefers_answer_correctness_when_judged() -> None:
    item = _item("q1", "问题一", gold=("A",), primary="A")
    unjudged = aggregate([score_question(item, ["A"], k=5), score_question(item, ["B"], k=5)], k=5)
    assert unjudged.answer_correctness is None
    assert unjudged.overall_metric == "recall@5"
    assert unjudged.overall_score == unjudged.recall_at_k == 0.5
    assert unjudged.hit_rate == 0.5

    hit = replace(score_question(item, ["A"], k=5), answer_correctness=1.0)
    miss = replace(score_question(item, ["B"], k=5), answer_correctness=0.4)
    judged = aggregate([hit, miss], k=5)
    assert judged.answered == 2
    assert judged.answer_correctness == pytest.approx(0.7)
    assert judged.overall_metric == "answer_correctness"
    assert judged.overall_score == pytest.approx(0.7)

    with pytest.raises(ValueError, match="评估结果为空"):
        aggregate([], k=5)


def test_chunk_digest_matches_citation_fields() -> None:
    chunk = _chunk(0, "早餐说明")
    digest = chunk_digest(chunk)
    assert digest["chunk_id"] == str(chunk.chunk_id)
    assert digest["document_title"] == "早餐说明"
    assert digest["ordinal"] == 0
    assert digest["score"] == round(chunk.score, 6)


def test_parse_score_accepts_json_keyword_and_ten_point_scales() -> None:
    assert parse_score('{"score": 0.9}') == 0.9
    assert parse_score('```json\n{"score": 1}\n```') == 1.0
    assert parse_score("score: 8.5") == 0.85
    assert parse_score("得分为 0.7，理由略") == 0.7
    assert parse_score("完全正确") is None
    assert parse_score('{"score": 12}') is None


def test_evaluate_items_scores_retrieval_and_judged_answers() -> None:
    items = [
        _item("q1", "早餐几点", gold=("早餐说明", "常见问题"), primary="早餐说明"),
        _item("q2", "WiFi 密码", gold=("网络说明",), primary="网络说明"),
    ]
    retriever = FakeRetriever(
        {"早餐几点": ["早餐说明", "常见问题", "早餐说明"], "WiFi 密码": ["网络说明"]}
    )
    evaluator = FakeEvaluator({"q1": 0.8, "q2": None})
    report = asyncio.run(
        evaluate_items(
            items=items,
            k=2,
            kb_ids={"hotel-guest": UUID(int=1)},
            content_fingerprint="sha256:content",
            retriever=retriever,
            evaluator=evaluator,
            dataset_path="unit.jsonl",
        )
    )

    assert retriever.calls == [("早餐几点", 2), ("WiFi 密码", 2)]
    assert evaluator.answers == ["答案:q1", "答案:q2"]
    first = report.outcomes[0]
    assert first["retrieved_titles"] == ["早餐说明", "常见问题"]
    assert first["recall"] == 1.0 and first["primary_top1"] is True
    assert first["answer"] == "答案:q1" and first["answer_correctness"] == 0.8
    assert report.metrics["recall_at_k"] == 1.0
    assert report.metrics["answered"] == 1
    assert report.metrics["answer_correctness"] == 0.8
    assert report.metrics["overall_metric"] == "answer_correctness"
    assert report.metrics["overall_score"] == 0.8
    assert report.evaluator == "fake-judge"
    assert report.dataset_fingerprint.startswith("sha256:")
    assert report.content_fingerprint == "sha256:content"
    assert report.config_fingerprint != report.dataset_fingerprint
    assert report.retrieval_config["k"] == 2
    assert report.retrieval_config["kb_codes"] == ["hotel-guest"]
    assert report.retrieval_config["embedding"].startswith("dev-deterministic:")
    assert report.retrieval_config["reranker"] == "keyword"
    assert report.as_dict()["report_id"] is None


def test_evaluate_items_without_judge_uses_recall_and_marks_degraded() -> None:
    items = [_item("q1", "早餐几点", gold=("早餐说明",), primary="早餐说明")]
    retriever = FakeRetriever({"早餐几点": ["早餐说明"]}, degraded=True)
    report = asyncio.run(
        evaluate_items(
            items=items,
            k=10,
            kb_ids={"hotel-guest": UUID(int=1)},
            content_fingerprint="sha256:content",
            retriever=retriever,
            evaluator=None,
        )
    )
    assert report.evaluator is None
    assert report.metrics["answer_correctness"] is None
    assert report.metrics["overall_metric"] == "recall@10"
    assert report.metrics["overall_score"] == 1.0
    assert report.outcomes[0]["degraded"] is True
    assert "answer" not in report.outcomes[0]


def test_evaluate_items_rejects_unresolved_knowledge_base() -> None:
    items = [_item("q1", "早餐几点", gold=("早餐说明",), primary="早餐说明")]
    with pytest.raises(EvalRunError, match="未解析的知识库"):
        asyncio.run(
            evaluate_items(
                items=items,
                k=3,
                kb_ids={},
                content_fingerprint="sha256:content",
                retriever=FakeRetriever({}),
            )
        )
    with pytest.raises(EvalRunError, match="K 必须为正整数"):
        asyncio.run(
            evaluate_items(
                items=items,
                k=0,
                kb_ids={"hotel-guest": UUID(int=1)},
                content_fingerprint="sha256:content",
                retriever=FakeRetriever({}),
            )
        )


def test_retrieval_config_snapshot_flags_configured_reranker() -> None:
    configured = get_settings().model_copy(
        update={
            "kb_rerank_enabled": True,
            "model_name": "qwen-max",
            "model_base_url": "http://model.local/v1",
            "model_embedding_name": "bge-m3",
            "embedding_dimensions": 1024,
        }
    )
    snapshot = retrieval_config_snapshot(10, configured, ["hotel-guest"])
    assert snapshot["reranker"] == "llm:qwen-max"
    assert snapshot["embedding"] == "openai-compatible:bge-m3:1024"
