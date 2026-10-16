"""回填 KB 评估集的分块级黄金锚点（dev 工具，不进运行时）。

流程：按真实分块配置把样例文档切块 → 用 gold_answer 与候选分块的相似度选锚点 →
导出人工审核稿（Markdown，逐条列出选中项与备选、标注需复核项）→ 审核后 --apply 写回。

相似度取「片段命中」与「字符二元组 Dice」的逐片段最大值再平均：前者认逐字引用，
后者认改写表达，避免换字后的正确答案被误判为无覆盖。分块配置取自 Settings
（与线上入库同源），因此锚点 ordinal 与检索回填一致。

命令：
    python build_kb_gold_chunks.py                 # 只出审核稿与候选清单
    python build_kb_gold_chunks.py --apply         # 审核后写回 kb_faq.jsonl
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_settings
from app.knowledge.parsing import chunk_text, extract_text
from app.knowledge.seed import SAMPLE_README, SAMPLE_TITLES, SAMPLES_DIR

BACKEND_DIR = Path(__file__).resolve().parent
SET_PATH = BACKEND_DIR / "app" / "eval" / "kb_faq.jsonl"
REVIEW_DIR = BACKEND_DIR.parent / "docs" / "eval" / "review"

FRAGMENT_SPLIT = re.compile(r"[，,。；;！!？?、\n]")
MARKER_STRIP = re.compile(r"[\s#*\-`]+")
MIN_FRAGMENT_CHARS = 2
CONFIDENT_COVERAGE = 0.6
SELECT_FLOOR = 0.3
SELECT_RATIO = 0.85
MAX_SELECTED = 2

# 首选相似度低于阈值但已人工比对原文确认锚点正确（答案跨小节，被两个分块共同覆盖）。
VERIFIED_OK: dict[str, str] = {
    "service-07": "双锚点：常见问题「服务相关」与「备品与借用」均写明免费备品由机器人送达。",
    "service-08": "双锚点：两处都写明可借吹风机等物品、退房前归还即可。",
    "resched-08": "双锚点：答案由「退款」与「订单取消操作」两小节共同构成。",
    "safety-01": "首选 0.586 略低于阈值，与「突发事件处理流程」共同覆盖疏散与集合点。",
}


@dataclass(frozen=True)
class Chunk:
    title: str
    ordinal: int
    content: str
    normalized: str
    bigram_seq: tuple[str, ...]


@dataclass
class Candidate:
    title: str
    ordinal: int
    coverage: float
    preview: str


@dataclass
class ItemReview:
    item_id: str
    question: str
    gold_answer: str
    gold_titles: list[str]
    primary_title: str
    selected: list[Candidate] = field(default_factory=list)
    alternatives: list[Candidate] = field(default_factory=list)

    @property
    def confident(self) -> bool:
        if self.item_id in VERIFIED_OK:
            return True
        return bool(self.selected) and self.selected[0].coverage >= CONFIDENT_COVERAGE


def build_index() -> dict[str, list[Chunk]]:
    settings = get_settings()
    index: dict[str, list[Chunk]] = {}
    for path in sorted(SAMPLES_DIR.glob("*.md")):
        if path.name == SAMPLE_README:
            continue
        text = extract_text(source_type="markdown", data=path.read_bytes())
        drafts = chunk_text(
            text,
            max_chars=settings.kb_chunk_max_chars,
            overlap_chars=settings.kb_chunk_overlap_chars,
        )
        title = SAMPLE_TITLES.get(path.name, path.stem)
        index[title] = [
            Chunk(
                title=title,
                ordinal=draft.ordinal,
                content=draft.content,
                normalized=normalized,
                bigram_seq=_bigram_seq(normalized),
            )
            for draft in drafts
            for normalized in [_normalize(draft.content)]
        ]
    return index


def _normalize(text: str) -> str:
    return MARKER_STRIP.sub("", text)


def _bigram_seq(text: str) -> tuple[str, ...]:
    return tuple(text[index : index + 2] for index in range(len(text) - 1))


def _fragments(answer: str) -> list[str]:
    parts = [_normalize(part) for part in FRAGMENT_SPLIT.split(answer)]
    return [part for part in parts if len(part) >= MIN_FRAGMENT_CHARS]


def _as_str_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def review_item(payload: dict[str, object], index: dict[str, list[Chunk]]) -> ItemReview:
    gold_titles = _as_str_list(payload.get("gold_titles"))
    fragments = _fragments(str(payload.get("gold_answer", "")))
    candidates: list[Candidate] = []
    for title in gold_titles:
        for chunk in index.get(title, []):
            coverage = _coverage(chunk, fragments)
            candidates.append(
                Candidate(
                    title=chunk.title,
                    ordinal=chunk.ordinal,
                    coverage=round(coverage, 4),
                    preview=_preview(chunk.content),
                )
            )
    candidates.sort(key=lambda item: (-item.coverage, item.ordinal))
    best = candidates[0].coverage if candidates else 0.0
    threshold = max(SELECT_FLOOR, best * SELECT_RATIO)
    selected = [item for item in candidates if item.coverage >= threshold][:MAX_SELECTED]
    alternatives = [item for item in candidates if item.coverage > 0 and item not in selected][:2]
    return ItemReview(
        item_id=str(payload.get("id", "")),
        question=str(payload.get("question", "")),
        gold_answer=str(payload.get("gold_answer", "")),
        gold_titles=gold_titles,
        primary_title=str(payload.get("primary_title", "")),
        selected=selected,
        alternatives=alternatives,
    )


def _coverage(chunk: Chunk, fragments: list[str]) -> float:
    """按片段长度加权的平均相似度：长片段信息量大，短片段（如「牙具」）不主导打分。"""
    if not fragments:
        return 0.0
    total = sum(len(fragment) for fragment in fragments)
    weighted = sum(len(fragment) * _fragment_score(fragment, chunk) for fragment in fragments)
    return weighted / total


def _fragment_score(fragment: str, chunk: Chunk) -> float:
    if fragment in chunk.normalized:
        return 1.0
    return _windowed_dice(fragment, chunk.bigram_seq)


def _windowed_dice(fragment: str, chunk_bigrams: tuple[str, ...]) -> float:
    """片段与分块在等长窗口上的最高二元组 Dice：认改写表达，避免换字即判 0。"""
    fragment_bigrams = Counter(_bigram_seq(fragment))
    width = sum(fragment_bigrams.values())
    if width == 0:
        return 0.0
    if len(chunk_bigrams) < width:
        return _dice(fragment_bigrams, Counter(chunk_bigrams))
    window = Counter(chunk_bigrams[:width])
    best = _overlap(fragment_bigrams, window) / width
    for index in range(width, len(chunk_bigrams)):
        dropped = chunk_bigrams[index - width]
        window[dropped] -= 1
        if not window[dropped]:
            del window[dropped]
        window[chunk_bigrams[index]] += 1
        score = _overlap(fragment_bigrams, window) / width
        if score > best:
            best = score
            if best == 1.0:
                break
    return best


def _dice(left: Counter[str], right: Counter[str]) -> float:
    total = sum(left.values()) + sum(right.values())
    return 2 * _overlap(left, right) / total if total else 0.0


def _overlap(left: Counter[str], right: Counter[str]) -> int:
    return sum((left & right).values())


def _preview(content: str, limit: int = 90) -> str:
    flat = re.sub(r"\s+", " ", content).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_review(reviews: list[ItemReview], index: dict[str, list[Chunk]]) -> str:
    low = [item for item in reviews if not item.confident]
    manual = [item for item in reviews if item.item_id in VERIFIED_OK]
    lines: list[str] = [
        "# KB 评估集分块级黄金锚点审核稿",
        "",
        "由 `backend/build_kb_gold_chunks.py` 生成；锚点为 `(document_title, ordinal)`。",
        f"confident = 首选分块加权相似度 ≥ {CONFIDENT_COVERAGE}，或经人工核对原文确认。",
        "相似度 = 逐片段取「逐字命中」与「字符二元组 Dice」最大值，再按片段长度加权平均。",
        "",
        "## 汇总",
        "",
        f"- 题目数：{len(reviews)}",
        f"- 自动通过：{len(reviews) - len(low) - len(manual)}",
        f"- 人工核验：{len(manual)}",
        f"- 需复核：{len(low)}",
        "",
        "## 分块索引",
        "",
    ]
    for title in sorted(index):
        chunk_list = index[title]
        lines.append(
            f"- **{title}**（{len(chunk_list)} 块）：ordinals {_ordinal_range(chunk_list)}"
        )
    lines += ["", "## 逐题明细", ""]
    for item in reviews:
        flag = "OK" if item.confident else "REVIEW"
        lines.append(f"### [{flag}] {item.item_id} — {item.question}")
        lines.append("")
        lines.append(f"- 参考答案：{item.gold_answer}")
        lines.append(f"- 黄金标题：{'、'.join(item.gold_titles)}")
        note = VERIFIED_OK.get(item.item_id)
        if note:
            lines.append(f"- 人工核验：{note}")
        if item.selected:
            for candidate in item.selected:
                lines.append(
                    f"- 选中：`{candidate.title}` #{candidate.ordinal}"
                    f"（覆盖率 {candidate.coverage}）｜{candidate.preview}"
                )
        else:
            lines.append("- 选中：（无）")
        for candidate in item.alternatives:
            lines.append(
                f"- 备选：`{candidate.title}` #{candidate.ordinal}"
                f"（覆盖率 {candidate.coverage}）｜{candidate.preview}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _ordinal_range(chunks: list[Chunk]) -> str:
    ordinals = sorted(chunk.ordinal for chunk in chunks)
    return f"{ordinals[0]}–{ordinals[-1]}" if ordinals else "—"


def load_payloads() -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line in SET_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            payloads.append(json.loads(line))
    return payloads


def apply(payloads: list[dict[str, object]], reviews: list[ItemReview]) -> int:
    by_id = {review.item_id: review for review in reviews}
    written: list[str] = []
    for payload in payloads:
        review = by_id[str(payload.get("id", ""))]
        assert review is not None
        chunks = [
            {"document_title": item.title, "ordinal": item.ordinal} for item in review.selected
        ]
        payload.pop("gold_chunks", None)
        ordered: dict[str, object] = {}
        for key, value in payload.items():
            ordered[key] = value
            if key == "gold_answer":
                ordered["gold_chunks"] = chunks
        if "gold_chunks" not in ordered:
            ordered["gold_chunks"] = chunks
        written.append(json.dumps(ordered, ensure_ascii=False))
    SET_PATH.write_text("\n".join(written) + "\n", encoding="utf-8")
    return sum(1 for review in reviews if review.selected)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="回填 KB 评估集分块级黄金锚点")
    parser.add_argument("--apply", action="store_true", help="审核后写回 kb_faq.jsonl")
    args = parser.parse_args(argv)

    index = build_index()
    payloads = load_payloads()
    reviews = [review_item(payload, index) for payload in payloads]
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    (REVIEW_DIR / "kb-gold-chunks-review.md").write_text(
        render_review(reviews, index), encoding="utf-8"
    )
    candidates = {
        review.item_id: [
            {"document_title": item.title, "ordinal": item.ordinal, "coverage": item.coverage}
            for item in review.selected
        ]
        for review in reviews
    }
    (REVIEW_DIR / "kb-gold-chunks-candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    low = [review.item_id for review in reviews if not review.confident]
    print(f"题目数={len(reviews)} 自动通过={len(reviews) - len(low)} 需复核={len(low)}")
    if low:
        print("需复核：" + "、".join(low))
    print(f"审核稿：{REVIEW_DIR / 'kb-gold-chunks-review.md'}")
    if args.apply:
        filled = apply(payloads, reviews)
        print(f"已写回：{SET_PATH}（含锚点 {filled} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
