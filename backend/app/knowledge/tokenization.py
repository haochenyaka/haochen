"""中英混合的粗粒度切分：CJK 双字滑窗 + 拉丁字母数字串。

开发态嵌入与词面重排共用同一口径，保证「同一问题命中同一词元」的确定性。
"""

from __future__ import annotations

CJK_RANGE_START = "\u4e00"
CJK_RANGE_END = "\u9fff"


def is_cjk(char: str) -> bool:
    return CJK_RANGE_START <= char <= CJK_RANGE_END


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens: list[str] = []
    buffer: list[str] = []
    for char in lowered:
        if is_cjk(char):
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            continue
        if char.isalnum():
            buffer.append(char)
        elif buffer:
            tokens.append("".join(buffer))
            buffer = []
    if buffer:
        tokens.append("".join(buffer))
    cjk_chars = [char for char in lowered if is_cjk(char)]
    tokens.extend(
        "".join(cjk_chars[index : index + 2]) for index in range(max(0, len(cjk_chars) - 1))
    )
    return tokens
