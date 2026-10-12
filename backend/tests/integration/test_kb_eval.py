"""知识库评估集成测试（真实 PostgreSQL + 向量库替身）：真实检索指标、报告落库与基线作废。

覆盖链路：run_eval（解析知识库 → 内容指纹 → 逐题真实检索 → 指标）→ persist_report
（报告追加 + 同数据集旧报告按指纹作废）。向量库与嵌入走替身，检索事实与回填以 PG 为准。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.eval.kb_dataset import EvalItem
from app.eval.kb_runner import EvalRunError, persist_report, run_eval
from app.knowledge import retrieval as retrieval_module
from app.knowledge import task_handlers
from app.knowledge.embedding import DeterministicEmbeddings
from app.models.eval import KbEvalReport
from tests.integration.db_utils import run_with_session_factory
from tests.integration.knowledge_fakes import (
    FakeObjectStorage,
    FakeVectorStore,
    create_knowledge_base,
    index_document,
)

pytestmark = pytest.mark.integration

SessionFactoryArg = async_sessionmaker[AsyncSession]

BREAKFAST_DOC = (
    "# 早餐说明\n\n"
    "早餐时间是每天 7:00-9:30，地点在 1 楼晨光餐厅，凭房号入座，无需餐券。\n\n"
    "房价不含早餐，单独购买 38 元/位，1.2 米以下儿童 19 元/位。"
)
WIFI_DOC = (
    "# 网络说明\n\n"
    "WiFi 名称是 HaoChen-Guest，密码为 8 位数字，可在小程序「我的房间」页面查看。\n\n"
    "网络无需额外付费，单房间最多连接 6 台设备。"
)
POOL_DOC = "# 泳池说明\n\n本店暂无泳池，最近的公共游泳馆在前街 3 号。"


@pytest.fixture(autouse=True)
def _clean_fakes() -> None:
    FakeObjectStorage.reset()
    FakeVectorStore.reset()
    yield
    FakeObjectStorage.reset()
    FakeVectorStore.reset()


@pytest.fixture(autouse=True)
def _patch_external_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(task_handlers, "ObjectStorage", FakeObjectStorage)
    monkeypatch.setattr(task_handlers, "ChunkVectorStore", FakeVectorStore)
    monkeypatch.setattr(task_handlers, "build_embedding_provider", DeterministicEmbeddings)
    monkeypatch.setattr(retrieval_module, "ChunkVectorStore", FakeVectorStore)
    monkeypatch.setattr(retrieval_module, "build_embedding_provider", DeterministicEmbeddings)
    monkeypatch.setattr(get_settings(), "kb_chunk_max_chars", 120)
    # 评估不在测试里调用外部模型：无密钥时判定为纯检索指标（answer_correctness 记 null）
    monkeypatch.setattr(get_settings(), "model_api_key", "")


def _items() -> list[EvalItem]:
    return [
        EvalItem(
            id="q-breakfast",
            question="早餐几点开始？",
            kb_code="hotel-guest",
            gold_titles=("早餐说明",),
            primary_title="早餐说明",
            gold_answer="早餐时间是每天 7:00-9:30。",
            tags=("早餐",),
        ),
        EvalItem(
            id="q-wifi",
            question="WiFi 密码是多少？",
            kb_code="hotel-guest",
            gold_titles=("网络说明",),
            primary_title="网络说明",
            gold_answer="密码为 8 位数字，在小程序「我的房间」页面查看。",
            tags=("网络",),
        ),
    ]


async def _seed(factory: SessionFactoryArg, code: str) -> Any:
    kb_id = await create_knowledge_base(factory, code=code)
    await index_document(factory, kb_id=kb_id, title="早餐说明", data=BREAKFAST_DOC.encode())
    await index_document(factory, kb_id=kb_id, title="网络说明", data=WIFI_DOC.encode())
    return kb_id


async def _eval_and_persist(factory: SessionFactoryArg, *, code: str, k: int = 3) -> dict[str, Any]:
    async with factory() as session:
        # 评估集里的 kb_code 用测试建库的真实 code
        items = [replace(item, kb_code=code) for item in _items()]
        report = await run_eval(session, items=items, k=k, dataset_path="tests/kb_eval.jsonl")
        outcome = await persist_report(session, report)
        return {
            "report": report,
            "outcome": outcome,
            "rows": await _rows(session, report.dataset_fingerprint),
        }


async def _rows(session: AsyncSession, dataset_fingerprint: str) -> list[KbEvalReport]:
    stmt = (
        select(KbEvalReport)
        .where(KbEvalReport.dataset_fingerprint == dataset_fingerprint)
        .order_by(KbEvalReport.id)
        .execution_options(populate_existing=True)
    )
    return list((await session.execute(stmt)).scalars())


def test_run_eval_scores_real_retrieval_and_persists_baseline(runtime_db_url: str) -> None:
    code = f"eval-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        await _seed(factory, code)
        return await _eval_and_persist(factory, code=code)

    values = run_with_session_factory(runtime_db_url, _body)
    report = values["report"]
    outcome = values["outcome"]

    # 两题都在 top-3 内命中主文档：recall/precision/f1 均为 1，评判缺省时 overall 取 recall@3
    assert report.metrics["questions"] == 2
    assert report.metrics["recall_at_k"] == 1.0
    assert report.metrics["precision_at_k"] == 1.0
    assert report.metrics["f1_at_k"] == 1.0
    assert report.metrics["primary_top1_rate"] == 1.0
    assert report.metrics["answer_correctness"] is None
    assert report.metrics["overall_metric"] == "recall@3"
    assert report.evaluator is None
    assert [item["item_id"] for item in report.outcomes] == ["q-breakfast", "q-wifi"]
    breakfast = report.outcomes[0]
    assert breakfast["retrieved_titles"] == ["早餐说明"]
    assert breakfast["retrieved"][0]["document_title"] == "早餐说明"

    # run_eval 不落库：报告编号由 persist_report 返回，落库结果与报告内容一致
    assert report.report_id is None
    assert outcome.comparable_reports == ()
    assert outcome.superseded_reports == ()
    assert outcome.report_id is not None
    rows = values["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row.metrics["overall_score"] == 1.0
    assert row.k == 3
    assert row.questions == 2
    assert row.dataset_fingerprint == report.dataset_fingerprint
    assert row.dataset_path == "tests/kb_eval.jsonl"
    assert row.evaluator is None
    assert row.superseded_by is None and row.invalidated_reason is None
    assert len(row.outcomes) == 2


def test_content_change_invalidates_previous_baseline(runtime_db_url: str) -> None:
    code = f"eval-supersede-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> dict[str, Any]:
        kb_id = await _seed(factory, code)
        first = await _eval_and_persist(factory, code=code)
        # 内容变化：新增一篇文档，内容指纹随之改变
        await index_document(factory, kb_id=kb_id, title="泳池说明", data=POOL_DOC.encode())
        second = await _eval_and_persist(factory, code=code)
        # 无变化重跑：与新报告指纹一致，应记为可比且不作废任何报告
        third = await _eval_and_persist(factory, code=code)
        return {"first": first, "second": second, "third": third}

    values = run_with_session_factory(runtime_db_url, _body)
    first_id = values["first"]["outcome"].report_id
    second = values["second"]
    third = values["third"]

    # 第二份报告作废第一份：内容指纹变化是唯一原因（配置未变）
    assert second["outcome"].superseded_reports == (first_id,)
    assert second["outcome"].comparable_reports == ()
    assert second["report"].content_fingerprint != values["first"]["report"].content_fingerprint
    assert second["report"].config_fingerprint == values["first"]["report"].config_fingerprint

    # 第三份报告与第二份可比，且不作废任何报告（含已作废的第一份）
    second_id = second["outcome"].report_id
    assert third["outcome"].comparable_reports == (second_id,)
    assert third["outcome"].superseded_reports == ()

    rows = {row.id: row for row in values["third"]["rows"]}
    assert rows[first_id].superseded_by == second_id
    assert rows[first_id].invalidated_reason == "content_changed"
    assert rows[second_id].superseded_by is None
    # 历史指标不被改写：第一份报告仍保留当时的题目数
    assert rows[first_id].questions == 2
    assert len(values["third"]["rows"]) == 3


def test_run_eval_fails_fast_on_unknown_knowledge_base(runtime_db_url: str) -> None:
    # 用本次唯一的 code：集成库为会话级共享临时库，固定名可能已被种子或其它用例建出
    missing_code = f"kb-missing-{uuid4().hex[:8]}"

    async def _body(factory: SessionFactoryArg) -> str:
        async with factory() as session:
            with pytest.raises(EvalRunError, match="知识库不存在"):
                await run_eval(
                    session,
                    items=[replace(item, kb_code=missing_code) for item in _items()],
                    k=3,
                    dataset_path="tests/kb_eval.jsonl",
                )
        return "ok"

    assert run_with_session_factory(runtime_db_url, _body) == "ok"
