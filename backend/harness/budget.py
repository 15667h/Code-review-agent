"""token 估算与预算控制。

估算只需要“足够准到能触发压缩”，不需要精确 —— 真实用量以 API 返回的 usage 为准。
估算刻意偏高，宁可早压缩，也不要溢出被截断。

书中的触发阈值：上下文占用超过窗口的 80% 时激活压缩。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

from .types import Message

# CJK 表意文字 + 中日韩标点 + 全角符号：按 1 token/字
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\u3000-\u303f\uff00-\uffef]")

# 每条消息的结构开销（role、分隔符等）
MESSAGE_OVERHEAD = 6


def estimate_tokens(text: str) -> int:
    """粗估一段文本的 token 数。CJK 按 1 token/字，其余按约 3.5 字符/token。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    rest = len(text) - cjk
    return int(cjk * 1.0 + rest * 0.29) + 1


def estimate_message(message: Message) -> int:
    total = MESSAGE_OVERHEAD + estimate_tokens(message.content)
    for call in message.tool_calls:
        total += estimate_tokens(call.name)
        total += estimate_tokens(json.dumps(call.arguments, ensure_ascii=False))
    return total


def estimate_messages(messages: Iterable[Message]) -> int:
    return sum(estimate_message(message) for message in messages)


def estimate_tools(tools: list[dict]) -> int:
    """估算工具定义占用的 token。工具 schema 是静态前缀的一部分，必须计入预算。"""
    if not tools:
        return 0
    return estimate_tokens(json.dumps(tools, ensure_ascii=False))


class Budget:
    """上下文预算。

    ``should_compress`` 使用书中的 80% 阈值。早先还有一个 ``headroom`` 想在临近
    上限时给工具输出留空间，实际没有调用方（压缩由调用方显式触发），已删除 ——
    docstring 承诺了不存在的行为，比没有承诺更糟。
    """

    def __init__(self, window: int = 131_072, trigger_ratio: float = 0.8) -> None:
        if window <= 0:
            raise ValueError("window 必须为正数")
        self.window = window
        self.trigger_ratio = trigger_ratio

    @property
    def trigger_at(self) -> int:
        return int(self.window * self.trigger_ratio)

    def used(self, prefix_tokens: int, messages: list[Message]) -> int:
        return prefix_tokens + estimate_messages(messages)

    def should_compress(self, prefix_tokens: int, messages: list[Message]) -> bool:
        return self.used(prefix_tokens, messages) >= self.trigger_at

