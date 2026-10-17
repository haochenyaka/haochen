"""解析与分块单元测试：编码兜底、偏移可回溯、分块不丢内容。"""

from __future__ import annotations

import hashlib

import pytest

from app.knowledge.parsing import (
    ParseError,
    chunk_text,
    extract_text,
    source_type_for_filename,
)


def test_source_type_for_filename_maps_known_extensions() -> None:
    assert source_type_for_filename("faq.md") == "markdown"
    assert source_type_for_filename("FAQ.MARKDOWN") == "markdown"
    assert source_type_for_filename("policy.txt") == "text"
    assert source_type_for_filename("menu.pdf") == "pdf"
    assert source_type_for_filename("photo.png") is None
    assert source_type_for_filename("noextension") is None


def test_extract_text_decodes_utf8_and_gb18030() -> None:
    source = "早餐 7:00 开始"
    assert extract_text(source_type="text", data=source.encode()) == source
    assert extract_text(source_type="text", data=source.encode("gb18030")) == source


def test_extract_text_normalizes_line_endings_and_blank_lines() -> None:
    text = extract_text(source_type="markdown", data="# 标题\r\n\r\n\r\n\r\n正文  \r\n".encode())
    assert text == "# 标题\n\n正文"


def test_extract_text_rejects_unknown_type_and_empty_content() -> None:
    with pytest.raises(ParseError):
        extract_text(source_type="html", data=b"<p>hi</p>")
    with pytest.raises(ParseError):
        extract_text(source_type="text", data=b"   \n\n  ")
    with pytest.raises(ParseError):
        extract_text(source_type="pdf", data=b"not-a-pdf")


def test_chunk_offsets_point_back_to_source_text() -> None:
    text = (
        "皓晨酒店没有前台，办理入住与退房全部在小程序内完成。\n\n"
        "WiFi 名称是 HaoChen-Guest，密码在小程序「我的房间」查看。\n\n"
        "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅。"
    )
    drafts = chunk_text(text, max_chars=60, overlap_chars=10)
    assert [draft.ordinal for draft in drafts] == list(range(len(drafts)))
    for draft in drafts:
        assert text[draft.char_start : draft.char_end] == draft.content
        assert draft.content == draft.content.strip()
        assert draft.content_hash == hashlib.sha256(draft.content.encode("utf-8")).hexdigest()
        assert len(draft.content) <= 60
    assert _uncovered_non_space(text, drafts) == []


def test_chunk_text_covers_all_content_for_long_paragraphs() -> None:
    text = "无人酒店自助入住流程说明。" * 60
    drafts = chunk_text(text, max_chars=100, overlap_chars=20)
    assert len(drafts) > 1
    assert all(len(draft.content) <= 100 for draft in drafts)
    assert _uncovered_non_space(text, drafts) == []


def test_chunk_text_keeps_overlap_between_adjacent_chunks() -> None:
    text = "。".join(f"第{index}条政策说明" for index in range(1, 25))
    drafts = chunk_text(text, max_chars=60, overlap_chars=20)
    assert len(drafts) >= 2
    assert all(
        current.char_start < previous.char_end
        for previous, current in zip(drafts, drafts[1:], strict=False)
    )


def test_chunk_text_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError):
        chunk_text("正文", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("正文", max_chars=10, overlap_chars=10)
    with pytest.raises(ParseError):
        chunk_text("   \n\n  ")


def test_chunk_text_marks_chunk_ordinals_contiguous() -> None:
    drafts = chunk_text("政策一。\n\n政策二。\n\n政策三。", max_chars=8, overlap_chars=2)
    assert [draft.ordinal for draft in drafts] == list(range(len(drafts)))


def test_chunk_text_splits_on_markdown_sections() -> None:
    text = "# 说明\n\n## 早餐\n\n" + "早餐内容。" * 20 + "\n\n## 网络\n\n" + "网络内容。" * 20
    drafts = chunk_text(text, max_chars=100, overlap_chars=10)
    assert len(drafts) >= 3
    # 分块不跨小节：没有一块同时混入两个主题的正文，重叠区只在节内保留
    assert all(
        not ("早餐内容" in draft.content and "网络内容" in draft.content) for draft in drafts
    )
    assert [draft.ordinal for draft in drafts] == list(range(len(drafts)))
    for draft in drafts:
        assert text[draft.char_start : draft.char_end] == draft.content
        assert len(draft.content) <= 100
    assert _uncovered_non_space(text, drafts) == []


def test_chunk_text_attaches_title_only_heading_to_next_section() -> None:
    text = "# 皓晨酒店介绍\n\n## 酒店概况\n\n皓晨酒店是无人酒店。\n\n## 房型\n\n共有 12 间客房。"
    drafts = chunk_text(text, max_chars=700, overlap_chars=80)
    assert len(drafts) == 2
    assert drafts[0].content.startswith("# 皓晨酒店介绍")
    assert "酒店概况" in drafts[0].content and "房型" not in drafts[0].content
    assert "房型" in drafts[1].content


def _uncovered_non_space(text: str, drafts: list[object]) -> list[int]:
    covered: set[int] = set()
    for draft in drafts:
        covered.update(range(draft.char_start, draft.char_end))  # type: ignore[attr-defined]
    return [index for index, char in enumerate(text) if not char.isspace() and index not in covered]
