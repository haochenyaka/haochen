"""知识库评估运行器（CLI）：逐题读当前检索配置，指标聚合，基线报告落库并输出 JSON。

命令：
    python -m app.eval.kb_runner --set app/eval/kb_faq.jsonl --k 10 \
        --out ../docs/eval/kb-baseline.json

不变量：
- 逐题走真实 KnowledgeRetriever（Milvus 宽捞 + 重排 + PG 回填），top_n 固定为 K；未配置
  模型时不生成答案与评判，报告记 answer_correctness=null，overall_score 取 recall@K；
- 报告不可变：新报告落库时，同数据集下内容或配置指纹不一致的旧报告被标记作废
  （superseded_by + invalidated_reason），指纹一致的旧报告保持有效并记入可比清单；
- 可比性三要素：dataset_fingerprint（评估集）、content_fingerprint（文档 checksum、
  分块数与逐分块 content_hash）、config_fingerprint（检索参数 + 评判模型）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import SessionFactory
from app.eval.kb_dataset import EvalItem, dataset_fingerprint, fingerprint, load_eval_set
from app.eval.kb_judge import AnswerEvaluator, build_answer_evaluator
from app.eval.kb_metrics import QuestionOutcome, aggregate, chunk_digest, score_question
from app.knowledge.embedding import DEV_EMBEDDING_DIMENSIONS
from app.knowledge.retrieval import KnowledgeRetriever, RetrievedContext
from app.models.eval import KbEvalReport
from app.repositories import KbChunkRepository, KbDocumentRepository, KnowledgeBaseRepository
from app.repositories.eval_repository import KbEvalReportRepository

logger = logging.getLogger(__name__)

DEFAULT_SET_PATH = Path(__file__).parent / "kb_faq.jsonl"
DEFAULT_K = 10
# 内容指纹的文档覆盖上限：单库文档数超过该值时指纹只覆盖前 N 篇（当前量级远未触顶）
CONTENT_DOCUMENT_LIMIT = 500


class EvalRunError(RuntimeError):
    """评估无法执行：知识库缺失、题目引用未解析的库或 K 非法。"""


class EvalRetriever(Protocol):
    """评估只依赖 retrieve 的上下文输出，测试可注入替身。"""

    async def retrieve(
        self,
        query: str,
        *,
        kb_ids: Sequence[UUID] | None = None,
        top_n: int | None = None,
    ) -> RetrievedContext: ...


@dataclass(frozen=True)
class EvalReport:
    """一次评估的完整事实：指标 + 逐题明细 + 三个指纹 + 落库后的作废/可比清单。"""

    dataset_path: str
    k: int
    dataset_fingerprint: str
    content_fingerprint: str
    config_fingerprint: str
    evaluator: str | None
    retrieval_config: dict[str, Any]
    metrics: dict[str, Any]
    outcomes: list[dict[str, Any]]
    generated_at: str
    report_id: int | None = None
    comparable_reports: tuple[int, ...] = ()
    superseded_reports: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "dataset_path": self.dataset_path,
            "k": self.k,
            "dataset_fingerprint": self.dataset_fingerprint,
            "content_fingerprint": self.content_fingerprint,
            "config_fingerprint": self.config_fingerprint,
            "evaluator": self.evaluator,
            "retrieval_config": dict(self.retrieval_config),
            "metrics": dict(self.metrics),
            "comparable_reports": list(self.comparable_reports),
            "superseded_reports": list(self.superseded_reports),
            "outcomes": [dict(item) for item in self.outcomes],
        }

    def render(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2) + "\n"


@dataclass(frozen=True)
class PersistOutcome:
    report_id: int
    comparable_reports: tuple[int, ...]
    superseded_reports: tuple[int, ...]


def embedding_identity(settings: Settings) -> str:
    if settings.model_embedding_name:
        dimensions = settings.embedding_dimensions or "auto"
        return f"openai-compatible:{settings.model_embedding_name}:{dimensions}"
    return f"dev-deterministic:{DEV_EMBEDDING_DIMENSIONS}"


def reranker_identity(settings: Settings) -> str:
    if settings.kb_rerank_enabled and settings.model_name and settings.model_base_url:
        return f"llm:{settings.model_name}"
    return "keyword"


def retrieval_config_snapshot(
    k: int, settings: Settings, kb_codes: Sequence[str]
) -> dict[str, object]:
    """检索配置快照：读当前全局配置，报告里固化，作为可比性判定的一部分。"""
    return {
        "k": k,
        "kb_codes": sorted(kb_codes),
        "kb_recall_limit": settings.kb_recall_limit,
        "kb_rerank_top_n": settings.kb_rerank_top_n,
        "kb_context_max_chars": settings.kb_context_max_chars,
        "kb_context_max_chunks_per_document": settings.kb_context_max_chunks_per_document,
        "kb_chunk_max_chars": settings.kb_chunk_max_chars,
        "kb_chunk_overlap_chars": settings.kb_chunk_overlap_chars,
        "embedding": embedding_identity(settings),
        "reranker": reranker_identity(settings),
    }


async def collect_content_fingerprint(session: AsyncSession, kb_ids: Mapping[str, UUID]) -> str:
    """内容指纹：每库全部文档的标题、状态、checksum 与逐分块 content_hash。

    逐分块哈希是分块级黄金锚点（document_title, ordinal）的稳定性兜底：仅比对分块数会漏掉
    「改了内容但分块数不变」，导致锚点静默指向错误段落。
    """
    documents = KbDocumentRepository(session)
    chunks = KbChunkRepository(session)
    payload: list[dict[str, object]] = []
    for code, kb_id in sorted(kb_ids.items()):
        rows = await documents.list_by_kb(kb_id, limit=CONTENT_DOCUMENT_LIMIT)
        entries: list[dict[str, object]] = []
        for row in rows:
            chunk_rows = await chunks.list_by_document(row.id)
            entries.append(
                {
                    "title": row.title,
                    "status": row.status,
                    "checksum": row.checksum,
                    "chunk_count": row.chunk_count,
                    "chunks": [[chunk.ordinal, chunk.content_hash] for chunk in chunk_rows],
                }
            )
        payload.append(
            {
                "kb_code": code,
                "documents": sorted(entries, key=lambda item: str(item["title"])),
            }
        )
    return fingerprint(payload)


async def evaluate_items(
    *,
    items: Sequence[EvalItem],
    k: int,
    kb_ids: Mapping[str, UUID],
    content_fingerprint: str,
    retriever: EvalRetriever,
    settings: Settings | None = None,
    evaluator: AnswerEvaluator | None = None,
    dataset_path: str | Path = DEFAULT_SET_PATH,
) -> EvalReport:
    """纯评估路径（不触库）：逐题检索打分，可选生成答案并评判，最后宏观聚合。"""
    settings = settings or get_settings()
    if k <= 0:
        raise EvalRunError("K 必须为正整数")
    outcomes: list[QuestionOutcome] = []
    for item in items:
        kb_id = kb_ids.get(item.kb_code)
        if kb_id is None:
            raise EvalRunError(f"题目 {item.id} 引用了未解析的知识库：{item.kb_code}")
        context = await retriever.retrieve(item.question, kb_ids=[kb_id], top_n=k)
        outcome = score_question(
            item,
            [chunk.document_title for chunk in context.chunks],
            k=k,
            retrieved=[chunk_digest(chunk) for chunk in context.chunks],
            degraded=context.degraded,
        )
        if evaluator is not None:
            answer = await evaluator.answer(item, context.prompt_text)
            correctness = await evaluator.judge(item, answer)
            outcome = replace(outcome, answer=answer, answer_correctness=correctness)
        outcomes.append(outcome)
    metrics = aggregate(outcomes, k=k)
    config = retrieval_config_snapshot(k, settings, sorted(kb_ids))
    evaluator_name = evaluator.model_name if evaluator is not None else None
    return EvalReport(
        dataset_path=str(dataset_path),
        k=k,
        dataset_fingerprint=dataset_fingerprint(items),
        content_fingerprint=content_fingerprint,
        config_fingerprint=fingerprint({"retrieval": config, "evaluator": evaluator_name}),
        evaluator=evaluator_name,
        retrieval_config=config,
        metrics=metrics.as_dict(),
        outcomes=[outcome.as_dict() for outcome in outcomes],
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


async def run_eval(
    session: AsyncSession,
    *,
    items: Sequence[EvalItem],
    k: int = DEFAULT_K,
    settings: Settings | None = None,
    retriever: EvalRetriever | None = None,
    evaluator: AnswerEvaluator | None = None,
    dataset_path: str | Path = DEFAULT_SET_PATH,
) -> EvalReport:
    """完整评估路径：解析知识库 → 内容指纹 → 逐题评估（真实检索器 + 可选评判器）。"""
    settings = settings or get_settings()
    kb_ids = await _resolve_kb_ids(session, sorted({item.kb_code for item in items}))
    content_fp = await collect_content_fingerprint(session, kb_ids)
    active_retriever = retriever or KnowledgeRetriever(session, settings=settings)
    active_evaluator: AnswerEvaluator | None = (
        evaluator if evaluator is not None else build_answer_evaluator(settings)
    )
    return await evaluate_items(
        items=items,
        k=k,
        kb_ids=kb_ids,
        content_fingerprint=content_fp,
        retriever=active_retriever,
        settings=settings,
        evaluator=active_evaluator,
        dataset_path=dataset_path,
    )


async def persist_report(session: AsyncSession, report: EvalReport) -> PersistOutcome:
    """报告落库：同数据集下指纹不一致的旧报告就地作废（标记，不改写历史指标）。"""
    repository = KbEvalReportRepository(session)
    previous = await repository.list_by_dataset(report.dataset_fingerprint)
    comparable = tuple(
        row.id
        for row in previous
        if row.content_fingerprint == report.content_fingerprint
        and row.config_fingerprint == report.config_fingerprint
    )
    row = await repository.create(
        dataset_path=report.dataset_path,
        dataset_fingerprint=report.dataset_fingerprint,
        content_fingerprint=report.content_fingerprint,
        config_fingerprint=report.config_fingerprint,
        k=report.k,
        questions=len(report.outcomes),
        evaluator=report.evaluator,
        retrieval_config=report.retrieval_config,
        metrics=report.metrics,
        outcomes=report.outcomes,
    )
    superseded: list[int] = []
    for old in previous:
        if old.superseded_by is not None:
            continue
        if (
            old.content_fingerprint == report.content_fingerprint
            and old.config_fingerprint == report.config_fingerprint
        ):
            continue
        reason = _invalidation_reason(old, report)
        if await repository.mark_superseded(old.id, superseded_by=row.id, reason=reason):
            superseded.append(old.id)
    await session.commit()
    return PersistOutcome(
        report_id=row.id,
        comparable_reports=comparable,
        superseded_reports=tuple(superseded),
    )


async def _resolve_kb_ids(session: AsyncSession, codes: Sequence[str]) -> dict[str, UUID]:
    knowledge_bases = KnowledgeBaseRepository(session)
    resolved: dict[str, UUID] = {}
    for code in codes:
        kb = await knowledge_bases.get_by_code(code)
        if kb is None:
            raise EvalRunError(f"评估集引用的知识库不存在：{code}")
        resolved[code] = kb.id
    return resolved


def _invalidation_reason(old: KbEvalReport, report: EvalReport) -> str:
    content_changed = old.content_fingerprint != report.content_fingerprint
    config_changed = old.config_fingerprint != report.config_fingerprint
    if content_changed and config_changed:
        return "content_and_config_changed"
    return "content_changed" if content_changed else "config_changed"


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="知识库评估基线：逐题检索 + 指标聚合 + 报告落库（内容/配置变化强制作废旧基线）"
    )
    parser.add_argument(
        "--set", dest="set_path", default=str(DEFAULT_SET_PATH), help="JSONL 评估集路径"
    )
    parser.add_argument(
        "--k", type=int, default=DEFAULT_K, help="recall@K 的 K，同时作为重排截断条数"
    )
    parser.add_argument("--out", dest="out_path", default="", help="把报告 JSON 写入该文件")
    parser.add_argument("--no-persist", action="store_true", help="只评估不落库（试跑对比用）")
    return parser.parse_args(argv)


async def _execute(
    *, items: Sequence[EvalItem], k: int, dataset_path: Path, persist: bool
) -> EvalReport:
    async with SessionFactory() as session:
        report = await run_eval(session, items=items, k=k, dataset_path=dataset_path)
        if not persist:
            return report
        outcome = await persist_report(session, report)
    return replace(
        report,
        report_id=outcome.report_id,
        comparable_reports=outcome.comparable_reports,
        superseded_reports=outcome.superseded_reports,
    )


def _log_summary(report: EvalReport) -> None:
    metrics = report.metrics
    logger.info("评估集：%s（%s 题，K=%d）", report.dataset_path, metrics["questions"], report.k)
    logger.info("数据集指纹：%s", report.dataset_fingerprint)
    logger.info("内容指纹：%s", report.content_fingerprint)
    logger.info("配置指纹：%s", report.config_fingerprint)
    logger.info(
        "recall@%d=%s precision@%d=%s f1@%d=%s",
        report.k,
        metrics["recall_at_k"],
        report.k,
        metrics["precision_at_k"],
        report.k,
        metrics["f1_at_k"],
    )
    logger.info(
        "primary@1=%s 命中率=%s answer_correctness=%s",
        metrics["primary_top1_rate"],
        metrics["hit_rate"],
        metrics["answer_correctness"],
    )
    logger.info(
        "chunk_recall@%d=%s chunk_precision@%d=%s chunk_f1@%d=%s",
        report.k,
        metrics.get("chunk_recall_at_k"),
        report.k,
        metrics.get("chunk_precision_at_k"),
        report.k,
        metrics.get("chunk_f1_at_k"),
    )
    logger.info(
        "overall_score=%s（%s）评判模型=%s",
        metrics["overall_score"],
        metrics["overall_metric"],
        report.evaluator or "未配置，仅检索指标",
    )
    if report.report_id is not None:
        logger.info(
            "报告已落库：id=%d 可比旧报告=%s 已作废旧报告=%s",
            report.report_id,
            list(report.comparable_reports),
            list(report.superseded_reports),
        )


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    set_path = Path(args.set_path)
    items = load_eval_set(set_path)
    report = asyncio.run(
        _execute(
            items=items,
            k=args.k,
            dataset_path=set_path,
            persist=not args.no_persist,
        )
    )
    if args.out_path:
        out_path = Path(args.out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report.render(), encoding="utf-8")
        logger.info("报告已写入：%s", out_path)
    _log_summary(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
