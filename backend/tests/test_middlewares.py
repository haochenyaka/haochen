"""中间件流水线验证：工位顺序与配置、各工位行为、洋葱执行顺序与挂位语义。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.agents.middlewares import (
    STATION_ORDER,
    DuplicateToolCallError,
    MemoryEntry,
    MiddlewareConfig,
    MiddlewareConfigError,
    MiddlewarePipeline,
    ModelCallRequest,
    SkillActivationError,
    ToolApprovalRequired,
    ToolCallRequest,
    ToolDeniedError,
    ToolResult,
    build_middleware_pipeline,
    estimate_messages_tokens,
    estimate_text_tokens,
    scan_sensitive_plaintext,
)
from app.agents.middlewares.base import Station
from app.agents.middlewares.credential_guard import (
    PROMPT_CONSTRAINT,
    CredentialGuardSettings,
    CredentialGuardStation,
)
from app.agents.middlewares.filesystem import FilesystemSettings, FilesystemStation
from app.agents.middlewares.memory import MemorySettings, MemoryStation
from app.agents.middlewares.model_retry import ModelRetrySettings, ModelRetryStation
from app.agents.middlewares.patch_tool_calls import patch_tool_calls
from app.agents.middlewares.skills import SkillsSettings, SkillsStation
from app.agents.middlewares.summary import SummarizationStation, SummarySettings
from app.agents.middlewares.token_usage import TokenUsageStation
from app.agents.middlewares.tool_approval import ApprovalDecision, ToolApprovalStation
from app.agents.middlewares.tool_guard import ToolGuardStation
from app.agents.skills import SkillCatalog, SkillDocument
from app.config import get_settings

BASELINE_ORDER = (
    "steer",
    "filesystem",
    "credential_guard",
    "skills",
    "memory",
    "summarization",
    "patch_tool_calls",
    "model_retry",
    "tool_guard",
    "token_usage",
    "tool_approval",
)

PLACEHOLDER_KEYS = ("steer",)
ACTIVE_KEYS = (
    "credential_guard",
    "filesystem",
    "skills",
    "summarization",
    "patch_tool_calls",
    "model_retry",
    "tool_guard",
    "token_usage",
    "tool_approval",
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_station_order_matches_baseline() -> None:
    pipeline = build_middleware_pipeline(config={})
    assert STATION_ORDER == BASELINE_ORDER
    assert [station.key for station in pipeline.stations] == list(BASELINE_ORDER)
    assert all(station.label for station in pipeline.stations)


def test_default_switches_keep_guard_slots_closed() -> None:
    pipeline = build_middleware_pipeline(config={})
    for key in PLACEHOLDER_KEYS:
        assert pipeline.station(key).enabled is False
    for key in ACTIVE_KEYS:
        assert pipeline.station(key).enabled is True
    assert pipeline.station("memory").enabled is False


def test_config_mapping_overrides_and_rejects_unknown() -> None:
    config = MiddlewareConfig.from_mapping(
        {
            "steer": {"enabled": True},
            "summarization": {"threshold_tokens": 500, "keep_messages": 2},
            "skills": {"preload": ["checkin-flow"], "dependencies": {"checkin-flow": ["base"]}},
        }
    )
    assert config.steer.enabled is True
    assert config.summarization.threshold_tokens == 500
    assert config.summarization.keep_messages == 2
    assert config.skills.preload == ("checkin-flow",)
    assert config.skills.dependencies == {"checkin-flow": ("base",)}
    with pytest.raises(MiddlewareConfigError):
        MiddlewareConfig.from_mapping({"unknown_station": {}})
    with pytest.raises(MiddlewareConfigError):
        MiddlewareConfig.from_mapping({"steer": {"unknown_field": 1}})


def test_config_defaults_come_from_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "summary_threshold", 1234)
    monkeypatch.setattr(settings, "large_result_threshold_bytes", 256)
    monkeypatch.setattr(settings, "model_retry_max_attempts", 7)
    config = MiddlewareConfig.from_mapping({"summarization": {"keep_messages": 3}})
    assert config.summarization.threshold_tokens == 1234
    assert config.summarization.keep_messages == 3
    assert config.filesystem.offload_threshold_bytes == 256
    assert config.model_retry.max_attempts == 7


def test_middleware_pipeline_rejects_duplicate_keys() -> None:
    trace: list[str] = []
    probe = type("Probe", (ProbeStation,), {"key": "probe"})("probe", trace)
    with pytest.raises(MiddlewareConfigError):
        MiddlewarePipeline([probe, probe])


class ProbeStation(Station):
    """洋葱顺序探针：记录前置、调用包裹与后置钩子的执行次序。"""

    def __init__(self, name: str, trace: list[str]) -> None:
        super().__init__()
        self.name = name
        self.trace = trace

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        self.trace.append(f"{self.name}.prepare")
        return request

    async def call_model(self, request: ModelCallRequest, call: Any) -> AIMessage:
        self.trace.append(f"{self.name}.call.in")
        reply = await call()
        self.trace.append(f"{self.name}.call.out")
        return reply

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        self.trace.append(f"{self.name}.after_tool")
        return result


def test_onion_order_forward_prepare_reverse_after() -> None:
    trace: list[str] = []
    first = type("Probe", (ProbeStation,), {"key": "probe-a"})("a", trace)
    second = type("Probe", (ProbeStation,), {"key": "probe-b"})("b", trace)
    pipeline = MiddlewarePipeline([first, second])
    request = ModelCallRequest(messages=[HumanMessage(content="hi")])

    run(pipeline.prepare_model_call(request))
    assert trace == ["a.prepare", "b.prepare"]

    trace.clear()

    async def base() -> AIMessage:
        trace.append("base")
        return AIMessage(content="ok")

    reply = run(pipeline.invoke_model(request, base=base))
    assert reply.content == "ok"
    assert trace == ["a.call.in", "b.call.in", "base", "b.call.out", "a.call.out"]

    trace.clear()
    run(pipeline.after_tool(ToolCallRequest(name="t", args={}, call_id="c"), ToolResult("c", "x")))
    assert trace == ["b.after_tool", "a.after_tool"]


def test_pipeline_prepare_applies_station_effects(tmp_path: Path) -> None:
    pipeline = build_middleware_pipeline(config={}, workdir=tmp_path)
    big = "结果" * 10_000
    messages: list[BaseMessage] = [
        SystemMessage(content="系统提示"),
        AIMessage(content="", tool_calls=[{"id": "call-1", "name": "kb", "args": {}}]),
        ToolMessage(content=big, tool_call_id="call-1"),
    ]
    prepared = run(pipeline.prepare_model_call(ModelCallRequest(messages=messages)))
    offloaded = str(prepared.messages[2].content)
    assert "[大结果已卸载]" in offloaded and "sha256=" in offloaded
    assert pipeline.summarization_events == []
    usage = pipeline.token_usage
    assert usage is not None and usage["approx_context_tokens"] > 0
    assert usage["model_calls"] == 0


def test_disabled_station_is_skipped() -> None:
    pipeline = build_middleware_pipeline(config={"patch_tool_calls": {"enabled": False}})
    dangling = AIMessage(content="", tool_calls=[{"id": "dangling", "name": "x", "args": {}}])
    prepared = run(
        pipeline.prepare_model_call(
            ModelCallRequest(messages=[dangling, HumanMessage(content="你好")])
        )
    )
    assert prepared.messages[-2].tool_calls[0]["id"] == "dangling"
    assert isinstance(prepared.messages[-1], HumanMessage)


def test_filesystem_offloads_large_tool_message(tmp_path: Path) -> None:
    station = FilesystemStation(
        settings=FilesystemSettings(offload_threshold_bytes=64, preview_chars=32), workdir=tmp_path
    )
    big = "结果" * 100
    small = ToolMessage(content="ok", tool_call_id="call-1")
    large = ToolMessage(content=big, tool_call_id="call-2")
    prepared = run(
        station.prepare_model_call(
            ModelCallRequest(messages=[HumanMessage(content="hi"), small, large])
        )
    )
    assert prepared.messages[1] is small
    replaced = prepared.messages[2]
    assert isinstance(replaced, ToolMessage)
    assert replaced.tool_call_id == "call-2"
    assert "[大结果已卸载]" in str(replaced.content)

    directory = tmp_path / "outputs" / "large_tool_results"
    files = list(directory.iterdir())
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == big

    run(station.prepare_model_call(ModelCallRequest(messages=[large])))
    assert len(list(directory.iterdir())) == 1

    result = run(
        station.after_tool(
            ToolCallRequest(name="query_kb", args={}, call_id="c3"), ToolResult("c3", big)
        )
    )
    assert "[大结果已卸载]" in str(result.content)


def test_filesystem_keeps_content_without_workdir() -> None:
    station = FilesystemStation(settings=FilesystemSettings(offload_threshold_bytes=8))
    result = run(
        station.after_tool(
            ToolCallRequest(name="t", args={}, call_id="c"), ToolResult("c", "x" * 100)
        )
    )
    assert result.content == "x" * 100


def test_patch_tool_calls_repairs_dangling_and_orphans() -> None:
    matched = {"id": "call-1", "name": "get_order", "args": {}}
    dangling = {"id": "call-2", "name": "get_room", "args": {}}
    patched = patch_tool_calls(
        [
            HumanMessage(content="查一下"),
            AIMessage(content="", tool_calls=[matched, dangling]),
            ToolMessage(content="订单 A", tool_call_id="call-1"),
            ToolMessage(content="无来源", tool_call_id="call-9"),
            AIMessage(content="订单 A 已确认"),
        ]
    )
    assert [type(message) for message in patched] == [
        HumanMessage,
        AIMessage,
        ToolMessage,
        AIMessage,
    ]
    assert [call["id"] for call in patched[1].tool_calls] == ["call-1"]

    stripped = patch_tool_calls(
        [HumanMessage(content="x"), AIMessage(content="", tool_calls=[dangling])]
    )
    assert len(stripped) == 1

    deduped = patch_tool_calls(
        [
            AIMessage(content="", tool_calls=[matched]),
            ToolMessage(content="a", tool_call_id="call-1"),
            ToolMessage(content="b", tool_call_id="call-1"),
        ]
    )
    assert len(deduped) == 2


def test_model_retry_backoff_cap_and_give_up() -> None:
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    attempts = {"count": 0}

    async def flaky() -> AIMessage:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise RuntimeError("boom")
        return AIMessage(content="ok")

    station = ModelRetryStation(
        settings=ModelRetrySettings(
            max_attempts=3,
            initial_backoff_seconds=0.5,
            backoff_multiplier=2.0,
            max_backoff_seconds=0.75,
        ),
        sleeper=sleeper,
    )
    reply = run(station.call_model(ModelCallRequest(messages=[]), flaky))
    assert reply.content == "ok"
    assert attempts["count"] == 3
    assert delays == [0.5, 0.75]

    async def always_fail() -> AIMessage:
        raise RuntimeError("boom")

    giving_up = ModelRetryStation(
        settings=ModelRetrySettings(max_attempts=2, initial_backoff_seconds=0.1), sleeper=sleeper
    )
    with pytest.raises(RuntimeError):
        run(giving_up.call_model(ModelCallRequest(messages=[]), always_fail))
    assert delays == [0.5, 0.75, 0.1]


def test_token_usage_estimates_and_aggregates() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("abcd") == 1
    assert estimate_text_tokens("你好") == 2

    station = TokenUsageStation()
    messages: list[BaseMessage] = [SystemMessage(content="你是前台"), HumanMessage(content="你好")]
    request = run(station.prepare_model_call(ModelCallRequest(messages=messages)))
    assert station.summary()["approx_context_tokens"] == estimate_messages_tokens(messages)

    async def cached() -> AIMessage:
        return AIMessage(
            content="a",
            usage_metadata={
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 96},
            },
        )

    async def plain() -> AIMessage:
        return AIMessage(
            content="b",
            usage_metadata={
                "input_tokens": 50,
                "output_tokens": 5,
                "total_tokens": 55,
                "input_token_details": {"cache_read": 0},
            },
        )

    run(station.call_model(request, cached))
    run(station.call_model(request, plain))
    summary = station.summary()
    assert summary["prompt_tokens"] == 150
    assert summary["cached_prompt_tokens"] == 96
    assert summary["completion_tokens"] == 15
    assert summary["model_calls"] == 2

    async def no_usage() -> AIMessage:
        return AIMessage(content="c")

    empty = TokenUsageStation()
    run(empty.call_model(ModelCallRequest(messages=[]), no_usage))
    assert empty.summary()["prompt_tokens"] is None
    assert empty.summary()["model_calls"] == 1


class FakeTool:
    def __init__(self, name: str, skill: str | None = None) -> None:
        self.name = name
        self.metadata = {"skill": skill} if skill else {}


def fake_catalog(*slugs: str) -> SkillCatalog:
    return SkillCatalog(
        documents={
            slug: SkillDocument(
                slug=slug,
                name=slug,
                description=f"{slug} 测试用",
                tool_dependencies=(),
                mcp_dependencies=(),
                skill_dependencies=(),
                instructions=f"{slug} 正文说明",
                path=Path("/skills") / slug / "SKILL.md",
            )
            for slug in slugs
        }
    )


def test_skills_gate_tools_by_activation_closure() -> None:
    station = SkillsStation(
        settings=SkillsSettings(
            preload=("checkin-flow",), dependencies={"checkin-flow": ("base",)}
        ),
        catalog=fake_catalog("checkin-flow", "base", "checkout-flow"),
    )
    assert station.active == frozenset({"checkin-flow", "base"})
    tools = [
        FakeTool("issue_password", "checkin-flow"),
        FakeTool("kb_search", "base"),
        FakeTool("invoice", "checkout-flow"),
        FakeTool("plain"),
    ]
    prepared = run(station.prepare_model_call(ModelCallRequest(messages=[], tools=tools)))
    assert [tool.name for tool in prepared.tools] == ["issue_password", "kb_search", "plain"]

    with pytest.raises(SkillActivationError):
        SkillsStation(settings=SkillsSettings(preload=("missing",)), catalog=fake_catalog("base"))

    with pytest.raises(SkillActivationError):
        SkillsStation(settings=SkillsSettings(activated=("base",)), catalog=SkillCatalog())


def test_skills_preload_merges_resource_ids_and_renders_instructions() -> None:
    station = SkillsStation(
        settings=SkillsSettings(preload=("checkin-flow",)),
        catalog=fake_catalog("checkin-flow", "checkout-flow"),
        preload=("checkout-flow", "checkin-flow"),
    )
    assert station.active == frozenset({"checkin-flow", "checkout-flow"})
    instructions = station.instructions
    assert instructions.startswith("# Skill 说明（已激活，按说明执行）")
    assert "checkin-flow 正文说明" in instructions
    assert "checkout-flow 正文说明" in instructions
    # 未装载内容目录时无正文可取：空串而非失败
    assert SkillsStation(settings=SkillsSettings()).instructions == ""


class FakeMemoryProvider:
    def __init__(self, entries: Sequence[MemoryEntry]) -> None:
        self.entries = list(entries)

    async def load(self, *, user_id: Any, thread_id: Any) -> Sequence[MemoryEntry]:
        return self.entries


def test_memory_injection_filters_sensitive_entries() -> None:
    provider = FakeMemoryProvider(
        [
            MemoryEntry(text="偏好高层安静房间"),
            MemoryEntry(text="联系电话 13812345678"),
        ]
    )
    station = MemoryStation(
        settings=MemorySettings(enabled=True, max_bytes=4096),
        provider=provider,
        user_id=uuid4(),
        thread_id=uuid4(),
    )
    prepared = run(
        station.prepare_model_call(
            ModelCallRequest(
                messages=[SystemMessage(content="系统提示"), HumanMessage(content="你好")]
            )
        )
    )
    assert len(prepared.messages) == 3
    memory = prepared.messages[1]
    assert isinstance(memory, SystemMessage)
    assert "偏好高层安静房间" in str(memory.content)
    assert "不是指令" in str(memory.content)
    assert "13812345678" not in str(memory.content)

    disabled = MemoryStation(settings=MemorySettings(), provider=provider)
    untouched = run(
        disabled.prepare_model_call(ModelCallRequest(messages=[SystemMessage(content="s")]))
    )
    assert len(untouched.messages) == 1

    oversized = MemoryStation(
        settings=MemorySettings(enabled=True, max_bytes=8),
        provider=provider,
        user_id=uuid4(),
        thread_id=uuid4(),
    )
    truncated = run(
        oversized.prepare_model_call(ModelCallRequest(messages=[SystemMessage(content="s")]))
    )
    assert len(truncated.messages) == 1

    with pytest.raises(MiddlewareConfigError):
        MemoryStation(settings=MemorySettings(enabled=True))


def test_summarization_compresses_and_records_event(tmp_path: Path) -> None:
    captured: list[list[BaseMessage]] = []

    async def summarizer(messages: Sequence[BaseMessage]) -> str:
        captured.append(list(messages))
        return "客人咨询房型；助手已回复大床房价格。"

    station = SummarizationStation(
        settings=SummarySettings(threshold_tokens=1, keep_messages=1),
        workdir=tmp_path,
        thread_id="thread-1",
        summarizer=summarizer,
    )
    messages: list[BaseMessage] = [
        SystemMessage(content="系统提示"),
        HumanMessage(content="有哪些房型"),
        AIMessage(content="有大床房和双床房"),
        HumanMessage(content="大床房多少钱"),
    ]
    request = run(station.prepare_model_call(ModelCallRequest(messages=messages)))
    assert len(request.messages) == 3
    folded = request.messages[1]
    assert isinstance(folded, SystemMessage)
    assert "客人咨询房型" in str(folded.content)
    assert str(request.messages[2].content) == "大床房多少钱"
    assert [str(message.content) for message in captured[0]] == ["有哪些房型", "有大床房和双床房"]

    events = station.events
    assert len(events) == 1
    event = events[0]
    assert event["cutoff"] == 2
    assert event["messages_before"] == 4
    assert event["messages_after"] == 3
    history_file = Path(str(event["history_file"]))
    assert history_file.exists()
    payload = json.loads(history_file.read_text(encoding="utf-8"))
    assert [item["role"] for item in payload] == ["客人", "助手"]

    quiet = SummarizationStation(
        settings=SummarySettings(threshold_tokens=100_000, keep_messages=1),
        workdir=tmp_path,
        summarizer=summarizer,
    )
    untouched = run(
        quiet.prepare_model_call(ModelCallRequest(messages=[HumanMessage(content="hi")]))
    )
    assert len(untouched.messages) == 1
    assert quiet.events == []

    async def blank(messages: Sequence[BaseMessage]) -> str:
        return ""

    empty = SummarizationStation(
        settings=SummarySettings(threshold_tokens=1, keep_messages=1),
        workdir=tmp_path,
        summarizer=blank,
    )
    kept = run(
        empty.prepare_model_call(
            ModelCallRequest(messages=[HumanMessage(content="a"), HumanMessage(content="b")])
        )
    )
    assert len(kept.messages) == 2
    assert empty.events == []

    with pytest.raises(MiddlewareConfigError):
        run(
            SummarizationStation(
                settings=SummarySettings(threshold_tokens=1, keep_messages=1), workdir=tmp_path
            ).prepare_model_call(
                ModelCallRequest(messages=[HumanMessage(content="a"), HumanMessage(content="b")])
            )
        )


def test_tool_approval_blocks_dangerous_and_denies_by_policy() -> None:
    station = ToolApprovalStation()
    safe = ToolCallRequest(name="get_order", args={}, call_id="c1")
    run(station.before_tool(safe))

    dangerous = ToolCallRequest(
        name="rotate_password", args={}, call_id="c2", metadata={"dangerous": True}
    )
    with pytest.raises(ToolApprovalRequired):
        run(station.before_tool(dangerous))

    class DenyPolicy:
        def decide(self, call: ToolCallRequest) -> ApprovalDecision:
            return ApprovalDecision("deny", "测试拒绝")

    with pytest.raises(ToolDeniedError):
        run(ToolApprovalStation(policy=DenyPolicy()).before_tool(safe))


def test_credential_scan_skeleton_detects_plaintext() -> None:
    findings = scan_sensitive_plaintext(
        "门锁密码：483920，身份证 11010519491231002X，手机 13812345678"
    )
    assert {item.category for item in findings} == {"password", "id_card", "phone"}
    assert scan_sensitive_plaintext("今天天气不错，房间号 8302") == []
    assert scan_sensitive_plaintext("密码是六位数，不要告诉别人") == []


def test_credential_guard_masks_tool_results_and_assistant_messages() -> None:
    station = CredentialGuardStation()
    result = run(
        station.after_tool(
            ToolCallRequest(name="get_credential", args={}, call_id="c1"),
            ToolResult("c1", "房号 8302，门锁密码：839201，请妥善保管"),
        )
    )
    assert result.content == "房号 8302，门锁密码：[已打码]，请妥善保管"
    assert scan_sensitive_plaintext(str(result.content)) == []

    reply = run(
        station.after_model(
            ModelCallRequest(messages=[]), AIMessage(content="您的密码是 839201，请查收")
        )
    )
    assert reply.content == "您的密码：[已打码]，请查收"
    assert station.redactions == [
        {"stage": "tool_result", "categories": ["password"]},
        {"stage": "assistant_message", "categories": ["password"]},
    ]

    untouched = run(
        station.after_tool(
            ToolCallRequest(name="list_orders", args={}, call_id="c2"),
            ToolResult("c2", {"room_no": "8302", "status": "paid"}),
        )
    )
    assert "8302" in str(untouched.content)
    assert station.redactions[-1]["stage"] == "assistant_message"


def test_credential_guard_redacts_tool_args_without_touching_execution() -> None:
    pipeline = build_middleware_pipeline(config={})
    args = {"room_no": "8302", "password": "839201", "note": "门锁密码：839201"}
    masked = pipeline.redact_tool_args(args)
    assert masked == {"room_no": "8302", "password": "[已打码]", "note": "门锁密码：[已打码]"}
    assert args["password"] == "839201" and args["note"] == "门锁密码：839201"
    assert [item["stage"] for item in pipeline.credential_redactions] == ["tool_args", "tool_args"]

    closed = build_middleware_pipeline(config={"credential_guard": {"enabled": False}})
    assert closed.redact_tool_args(args) == args
    assert closed.credential_redactions == []


def test_credential_guard_sweeps_history_and_keeps_single_constraint() -> None:
    pipeline = build_middleware_pipeline(config={})
    prepared = run(
        pipeline.prepare_model_call(
            ModelCallRequest(
                messages=[
                    SystemMessage(content="系统提示"),
                    HumanMessage(content="上次的密码：839201 还能用吗"),
                    AIMessage(
                        content="",
                        tool_calls=[{"id": "c1", "name": "get_credential", "args": {}}],
                    ),
                    ToolMessage(content="当前密码是 839201", tool_call_id="c1"),
                ]
            )
        )
    )
    head = prepared.messages[0]
    assert isinstance(head, SystemMessage)
    assert str(head.content).startswith("系统提示")
    assert PROMPT_CONSTRAINT in str(head.content)
    assert str(prepared.messages[1].content) == "上次的密码：[已打码] 还能用吗"
    assert str(prepared.messages[3].content) == "当前密码：[已打码]"
    assert {item["stage"] for item in pipeline.credential_redactions} == {"context"}

    again = run(
        pipeline.prepare_model_call(ModelCallRequest(messages=[head, HumanMessage(content="嗯")]))
    )
    assert str(again.messages[0].content).count(PROMPT_CONSTRAINT) == 1

    quiet = build_middleware_pipeline(config={"credential_guard": {"prompt_constraint": False}})
    plain = run(
        quiet.prepare_model_call(ModelCallRequest(messages=[SystemMessage(content="系统提示")]))
    )
    assert str(plain.messages[0].content) == "系统提示"


def test_credential_guard_masks_before_summary_history_dump(tmp_path: Path) -> None:
    captured: list[list[BaseMessage]] = []

    async def summarizer(messages: Sequence[BaseMessage]) -> str:
        captured.append(list(messages))
        return "客人问过门锁凭证，已按下发流程处理。"

    pipeline = build_middleware_pipeline(
        config={"summarization": {"threshold_tokens": 1, "keep_messages": 1}},
        workdir=tmp_path,
        thread_id="thread-guard",
        summary_delegate=summarizer,
    )
    prepared = run(
        pipeline.prepare_model_call(
            ModelCallRequest(
                messages=[
                    SystemMessage(content="系统提示"),
                    HumanMessage(content="门锁密码：839201 还能用吗"),
                    AIMessage(content="已核对：密码：839201 当日有效"),
                    HumanMessage(content="好的"),
                ]
            )
        )
    )
    assert [str(message.content) for message in captured[0]] == [
        "门锁密码：[已打码] 还能用吗",
        "已核对：密码：[已打码] 当日有效",
    ]
    events = pipeline.summarization_events
    assert len(events) == 1
    dumped = Path(str(events[0]["history_file"])).read_text(encoding="utf-8")
    assert "839201" not in dumped
    assert scan_sensitive_plaintext(dumped) == []
    assert "已按下发流程处理" in str(prepared.messages[1].content)


def test_credential_guard_defaults_to_all_categories_and_rejects_unknown() -> None:
    station = CredentialGuardStation()
    text = "手机 13812345678，身份证 11010519491231002X"
    masked = run(
        station.after_tool(ToolCallRequest(name="t", args={}, call_id="c"), ToolResult("c", text))
    )
    # 出厂默认即打码三类：身份证保前 6 后 4、手机号保前 3 后 4，中间等长打码
    assert masked.content == "手机 138****5678，身份证 110105********002X"
    assert scan_sensitive_plaintext(str(masked.content)) == []
    assert station.redactions == [{"stage": "tool_result", "categories": ["id_card", "phone"]}]

    narrow = CredentialGuardStation(settings=CredentialGuardSettings(mask_categories=("password",)))
    kept = run(
        narrow.after_tool(ToolCallRequest(name="t", args={}, call_id="c"), ToolResult("c", text))
    )
    # 收窄为仅密码：手机号与身份证保留原文
    assert kept.content == text
    assert narrow.redactions == []

    with pytest.raises(MiddlewareConfigError):
        CredentialGuardStation(settings=CredentialGuardSettings(mask_categories=("mail",)))


SIDE_EFFECT_METADATA = {"read_only": False, "dangerous": False}
READ_ONLY_TOOL_METADATA = {"read_only": True, "dangerous": False}


def s_effect_call(args: dict[str, Any], call_id: str = "c1") -> ToolCallRequest:
    return ToolCallRequest(
        name="create_order", args=args, call_id=call_id, metadata=SIDE_EFFECT_METADATA
    )


def test_tool_guard_blocks_in_flight_and_reuses_completed() -> None:
    station = ToolGuardStation(run_id=uuid4())
    read_only = ToolCallRequest(
        name="list_room_types", args={}, call_id="c0", metadata=READ_ONLY_TOOL_METADATA
    )
    assert station.key_for(read_only) is None
    run(station.before_tool(read_only))
    assert station.reuse_tool_result(read_only) is None
    assert station.events == []

    call = s_effect_call({"room_type": "大床房"})
    run(station.before_tool(call))
    with pytest.raises(DuplicateToolCallError):
        run(station.before_tool(call))
    first = run(station.after_tool(call, ToolResult("c1", "订单已创建")))
    key = first.metadata["idempotency_key"]
    assert key.startswith("toolguard:")
    assert station.key_for(call) == key
    # 入参键序不影响幂等键
    assert station.key_for(s_effect_call({"room_type": "大床房"}, call_id="c8")) == key

    # 已完成：同参不同调用 id 复用既有结果
    again = s_effect_call({"room_type": "大床房"}, call_id="c9")
    reused = station.reuse_tool_result(again)
    assert reused is not None and reused.call_id == "c9"
    assert reused.content == "订单已创建"
    assert reused.metadata["idempotency_key"] == key
    # 复用在流水线里不再走 before_tool；同键仍可直接执行后的钩子（不再计 in-flight）
    run(station.before_tool(again))
    assert station.reuse_tool_result(again) is not None

    # 参数不同 → 键不同 → 不拦截
    other = s_effect_call({"room_type": "双床房"}, call_id="c3")
    assert station.reuse_tool_result(other) is None
    run(station.before_tool(other))

    # 失败释放键：允许重试
    failing = ToolCallRequest(
        name="cancel_order", args={"order_no": "A1"}, call_id="c4", metadata=SIDE_EFFECT_METADATA
    )
    run(station.before_tool(failing))
    run(station.after_tool(failing, ToolResult("c4", "失败", is_error=True)))
    run(station.before_tool(failing))
    assert station.reuse_tool_result(failing) is None

    assert [event["outcome"] for event in station.events] == ["blocked", "reused", "reused"]


def test_tool_guard_settings_and_pipeline_wiring() -> None:
    pipeline = build_middleware_pipeline(config={}, run_id=uuid4())
    assert pipeline.station("tool_guard").enabled is True
    call = s_effect_call({"room_type": "大床房"})
    assert pipeline.reuse_tool_result(call) is None
    run(pipeline.before_tool(call))
    run(pipeline.after_tool(call, ToolResult("c1", "订单已创建")))
    reused = pipeline.reuse_tool_result(s_effect_call({"room_type": "大床房"}, call_id="c2"))
    assert reused is not None and reused.content == "订单已创建"
    assert [event["outcome"] for event in pipeline.tool_guard_events] == ["reused"]

    closed = build_middleware_pipeline(config={"tool_guard": {"enabled": False}}, run_id=uuid4())
    assert closed.reuse_tool_result(call) is None
    run(closed.before_tool(call))
    run(closed.before_tool(call))
    assert closed.tool_guard_events == []
