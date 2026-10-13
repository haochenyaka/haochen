"""工位 3 CredentialGuard：凭证明文打码、下发通道与阻断。

打码落在各持久化边界上：工具回包进入检查点与后续模型请求之前、助手消息入库之前、压缩
历史落盘之前、工具入参进入审计与事件投影之前；生成中的流式增量不审查——下发与提醒场景
的实时回复可以带当日密码，但任何落库副本都是打码形态（明文不落数据库、不进消息历史）。
默认打码三类：密码整值占位，身份证/手机号保留前 6 后 4 / 前 3 后 4、中间等长打码（最小化
且保留可读）；打码类别可配置且未知类别显式失败。

下发通道（Request-scoped delivery conduit）：标了 credential_delivery 的工具回包在打码
之前先取走明文，暂存在工位内存里，只并入下一次模型请求的开头系统消息（请求态副本，不进
状态、检查点与事件流），模型据此在当轮回复中告知客人；已下发的明文另存一份 Run 级清单，
对通过本工位的其它副本做精确串打码兜底。

记忆与知识库的阻断靠两道力：本工位的打码让历史与工具回包不再携带明文，Memory 工位的
敏感条目过滤整条丢弃。位置约束由装配顺序保证——本工位先于 Memory 与 Summarization，
后置钩子按逆序先于 filesystem 落盘，大结果卸载与压缩历史拿到的都是打码后的文本。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, SystemMessage

from app.agents.middlewares.base import (
    MiddlewareConfigError,
    ModelCallRequest,
    Station,
    ToolCallRequest,
    ToolResult,
    leading_system_count,
)
from app.agents.middlewares.filesystem import content_text

logger = logging.getLogger(__name__)

_PASSWORD_VALUE = r"(?=[A-Za-z0-9@#*_\-]*\d)[A-Za-z0-9@#*_\-]{4,}"
_PASSWORD_MARKER = r"(?:密码|口令|password|passcode)\s*(?:是|为|[:：=])\s*"

PASSWORD_VALUE_PATTERN = re.compile(
    rf"{_PASSWORD_MARKER}({_PASSWORD_VALUE})",
    re.IGNORECASE,
)
"""凭证明文定位（带捕获组）：下发通道据此从原始工具回包取明文，与打码形态同一口径。"""

SENSITIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("password", re.compile(rf"{_PASSWORD_MARKER}{_PASSWORD_VALUE}", re.IGNORECASE)),
    ("id_card", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
)
"""敏感形态：password 要求口令值含数字且长度≥4，避免把「密码是六位数」这类描述当命中。"""

CREDENTIAL_DELIVERY_METADATA_KEY = "credential_delivery"
"""工具注册元数据键：该工具回包可能携带待下发凭证明文（仅下发类工具可标）。"""

DELIVERY_NOTICE_TEMPLATE = (
    "本次需向客人下发的开门密码：{code}\n"
    "请在本轮回复中把该密码告知客人本人，并说明：旧密码已立即失效、密码仅本次展示、"
    "请客人及时保存。不得把密码写入任何工具入参、记忆或备注，也不要向他人转述。"
)
"""一次下发提示：只并入下一次模型请求，请求态副本不进入状态与检查点。"""

SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passwd|passcode|secret|token|密码|口令", re.IGNORECASE
)
"""入参键名疑似凭据：整值打码（值本身无字段标记，内容扫描判不出来）。"""

MASK_TEXT = "[已打码]"
MASK_CHAR = "*"
PLACEHOLDER_BY_CATEGORY = {
    "password": f"密码：{MASK_TEXT}",
    "id_card": "[身份证已打码]",
    "phone": "[手机号已打码]",
}
"""整值占位文本：password 保留字段名便于阅读，值形态不再构成可命中的敏感串。

身份证与手机号默认走"保留前后缀、中间打码"（见 PARTIAL_KEEP），占位文本仅作
无部分打码配置时的兜底。"""

PARTIAL_KEEP: dict[str, tuple[int, int]] = {"id_card": (6, 4), "phone": (3, 4)}
"""部分打码的保留位数（前缀, 后缀）：身份证保前 6 后 4，手机号保前 3 后 4，
中间以 MASK_CHAR 等长替换（如 138****5678 / 110101********1234）。"""

DEFAULT_MASK_CATEGORIES: tuple[str, ...] = ("password", "id_card", "phone")
SAMPLE_MAX_CHARS = 64

PROMPT_CONSTRAINT = (
    "安全约束：不得复述、推断或向客人索要历史会话中的密码；历史中的密码一律以打码形态"
    "呈现。当日密码只在下发与提醒场景告知本人，其它场景不主动提及。"
)


@dataclass(frozen=True)
class CredentialGuardSettings:
    enabled: bool = True
    mask_categories: tuple[str, ...] = DEFAULT_MASK_CATEGORIES
    prompt_constraint: bool = True


@dataclass(frozen=True)
class SensitiveMatch:
    """命中片段：category 为敏感类别，sample 截断后的命中文本。"""

    category: str
    sample: str


def scan_sensitive_plaintext(text: str) -> list[SensitiveMatch]:
    """扫描明文敏感信息：密码字段、身份证、手机号；按命中顺序返回。"""
    matches: list[SensitiveMatch] = []
    for category, pattern in SENSITIVE_PATTERNS:
        for hit in pattern.finditer(text):
            matches.append(
                SensitiveMatch(category=category, sample=hit.group(0)[:SAMPLE_MAX_CHARS])
            )
    return matches


def _mask_segment(match: re.Match[str], category: str) -> str:
    """命中片段的重写：password 整值占位；身份证/手机号按 PARTIAL_KEEP 保留前后缀。

    部分打码是"最小化 + 可读"的取舍——号码本人仍可辨认，库内不再留存完整值；
    完整形态被破坏后不再匹配敏感正则（scan_sensitive_plaintext 零命中）。"""
    span = match.group(0)
    keep = PARTIAL_KEEP.get(category)
    if keep is None:
        return PLACEHOLDER_BY_CATEGORY[category]
    prefix, suffix = keep
    if len(span) <= prefix + suffix:
        return MASK_CHAR * len(span)
    middle = MASK_CHAR * (len(span) - prefix - suffix)
    return f"{span[:prefix]}{middle}{span[-suffix:]}"


def mask_sensitive_plaintext(
    text: str, *, categories: Sequence[str] = DEFAULT_MASK_CATEGORIES
) -> tuple[str, tuple[str, ...]]:
    """按类别打码：返回（打码文本、命中类别）；未命中返回原文与空元组。"""
    wanted = frozenset(categories)
    hits = sorted(
        (match.start(), match.end(), category, match)
        for category, pattern in SENSITIVE_PATTERNS
        if category in wanted
        for match in pattern.finditer(text)
    )
    if not hits:
        return text, ()
    pieces: list[str] = []
    used: list[str] = []
    cursor = 0
    for start, end, category, match in hits:
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        pieces.append(_mask_segment(match, category))
        cursor = end
        used.append(category)
    pieces.append(text[cursor:])
    return "".join(pieces), tuple(used)


class CredentialGuardStation(Station):
    key = "credential_guard"
    label = "CredentialGuard 凭证保护"

    def __init__(self, *, settings: CredentialGuardSettings | None = None) -> None:
        resolved = settings or CredentialGuardSettings()
        super().__init__(enabled=resolved.enabled)
        known = {category for category, _ in SENSITIVE_PATTERNS}
        unknown = sorted(set(resolved.mask_categories) - known)
        if unknown:
            raise MiddlewareConfigError(f"未知打码类别：{unknown}（可选：{sorted(known)}）")
        self._settings = resolved
        self._redactions: list[dict[str, Any]] = []
        self._pending_delivery: str | None = None
        self._delivered_values: list[str] = []

    @property
    def redactions(self) -> list[dict[str, Any]]:
        """打码记录：只保留阶段与命中类别，不含命中内容。"""
        return list(self._redactions)

    @property
    def pending_delivery(self) -> bool:
        """是否存在尚未进入回复窗口的下发明文（不暴露内容本身）。"""
        return self._pending_delivery is not None

    def redact_tool_args(self, args: dict[str, Any]) -> None:
        """审计与投影入参就地打码：键名疑似凭据整值打码，其余按内容形态打码。"""
        for key, value in list(args.items()):
            if not isinstance(value, str) or not value:
                continue
            if SENSITIVE_KEY_PATTERN.search(key):
                args[key] = MASK_TEXT
                self._record("tool_args", ("password",))
                continue
            masked, used = self._mask("tool_args", value)
            if used:
                args[key] = masked

    async def prepare_model_call(self, request: ModelCallRequest) -> ModelCallRequest:
        for index, message in enumerate(request.messages):
            masked = self._mask_message(message, stage="context")
            if masked is not message:
                request.messages[index] = masked
        if self._settings.prompt_constraint:
            _attach_constraint(request.messages)
        if self._pending_delivery is not None:
            _attach_delivery_notice(request.messages, self._pending_delivery)
        return request

    async def after_model(self, request: ModelCallRequest, reply: AIMessage) -> AIMessage:
        content, changed = self._mask_content(reply.content, stage="assistant_message")
        if not getattr(reply, "tool_calls", None):
            # 工具循环已到面向客人的回复出口：待发提示与精确串兜底随之收束
            self._pending_delivery = None
            self._delivered_values.clear()
        return reply.model_copy(update={"content": content}) if changed else reply

    async def after_tool(self, call: ToolCallRequest, result: ToolResult) -> ToolResult:
        raw = content_text(result.content)
        if (call.metadata or {}).get(CREDENTIAL_DELIVERY_METADATA_KEY):
            self._capture_delivery(raw)
        masked, used = self._mask("tool_result", raw)
        if used:
            result.content = masked
        return result

    def _capture_delivery(self, text: str) -> None:
        """下发通道取明文：标了 credential_delivery 的工具回包，打码前取走一次性明文。"""
        values = [match.group(1) for match in PASSWORD_VALUE_PATTERN.finditer(text)]
        if not values:
            logger.warning("下发工具回包未定位到明文，未进入下发通道")
            return
        self._pending_delivery = values[-1]
        for value in values:
            if value not in self._delivered_values:
                self._delivered_values.append(value)
        self._record("credential_delivery", ("password",))

    def _mask(self, stage: str, text: str) -> tuple[str, tuple[str, ...]]:
        if not text:
            return text, ()
        stripped, exact_hit = self._strip_delivered(text)
        if exact_hit:
            self._record(stage, ("password",))
        masked, used = mask_sensitive_plaintext(stripped, categories=self._settings.mask_categories)
        if used:
            self._record(stage, used)
        if exact_hit:
            used = (*used, "password")
        return masked, used

    def _strip_delivered(self, text: str) -> tuple[str, bool]:
        """精确串兜底：已下发明文即使脱离字段标记出现，也一并打码。"""
        hit = False
        for value in self._delivered_values:
            if value and value in text:
                text = text.replace(value, MASK_TEXT)
                hit = True
        return text, hit

    def _mask_message(self, message: BaseMessage, *, stage: str) -> BaseMessage:
        content, changed = self._mask_content(message.content, stage=stage)
        return message.model_copy(update={"content": content}) if changed else message

    def _mask_content(self, content: Any, *, stage: str) -> tuple[Any, bool]:
        """消息内容打码：字符串直改，内容块只改文本块，其它形态原样保留。"""
        if isinstance(content, str):
            masked, used = self._mask(stage, content)
            return (masked, True) if used else (content, False)
        if isinstance(content, list):
            blocks: list[Any] = []
            changed = False
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    masked, used = self._mask(stage, block["text"])
                    if used:
                        blocks.append({**block, "text": masked})
                        changed = True
                        continue
                blocks.append(block)
            if changed:
                return blocks, True
        return content, False

    def _record(self, stage: str, categories: tuple[str, ...]) -> None:
        self._redactions.append({"stage": stage, "categories": sorted(set(categories))})
        logger.warning(
            "命中敏感明文并已打码：stage=%s categories=%s hits=%d",
            stage,
            sorted(set(categories)),
            len(categories),
        )


def _attach_delivery_notice(messages: list[BaseMessage], code: str) -> None:
    """下发提示并入开头系统消息：与约束同理，不新增消息条数、不改变历史索引。"""
    notice = DELIVERY_NOTICE_TEMPLATE.format(code=code)
    if not messages:
        messages.append(SystemMessage(content=notice))
        return
    head = leading_system_count(messages)
    if head == 0:
        messages.insert(0, SystemMessage(content=notice))
        return
    first = messages[0]
    text = first.content if isinstance(first.content, str) else ""
    combined = f"{text}\n\n{notice}" if text else notice
    messages[0] = first.model_copy(update={"content": combined})


def _attach_constraint(messages: list[BaseMessage]) -> None:
    """约束并入开头系统提示：不新增消息，避免改变消息条数与历史索引。"""
    if not messages:
        messages.append(SystemMessage(content=PROMPT_CONSTRAINT))
        return
    head = leading_system_count(messages)
    if head == 0:
        messages.insert(0, SystemMessage(content=PROMPT_CONSTRAINT))
        return
    first = messages[0]
    text = first.content if isinstance(first.content, str) else ""
    if PROMPT_CONSTRAINT in text:
        return
    combined = f"{text}\n\n{PROMPT_CONSTRAINT}" if text else PROMPT_CONSTRAINT
    messages[0] = first.model_copy(update={"content": combined})
