from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# 4 个窄字符（ASCII）≈ 1 token
_CHARS_PER_TOKEN = 4

_ASCII_RUN = re.compile(r"[\x00-\x7f]+")


# 估算文本 token 数：非 ASCII 字符（汉字/假名/谚文/emoji 等）按 1 token/字符，
# ASCII 按 4 字符/1 token。对中文偏保守（宁可高估，压缩宁可早触发）。
def estimate_text(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(len(match.group()) for match in _ASCII_RUN.finditer(text))
    wide_chars = len(text) - ascii_chars
    return wide_chars + (ascii_chars + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


# 估算单条消息内容的 token 数（支持字符串与 content block 列表）
def estimate_content(content: Any) -> int:
    if isinstance(content, str):
        return estimate_text(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            total += estimate_text(str(block.get("text", "")))
        elif block_type == "thinking":
            total += estimate_text(str(block.get("thinking", "")))
        elif block_type == "tool_use":
            total += estimate_text(str(block.get("name", "")))
            total += estimate_text(_to_text(block.get("input", {})))
        elif block_type == "tool_result":
            total += estimate_text(_flatten(block.get("content", "")))
        total += 8  # 每个 block 的结构开销（type/id 等字段）
    return total


# 估算消息列表的 token 数
def estimate_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_content(msg.get("content")) + 4 for msg in messages)


# 估算一次请求体（system + messages + tools）的 token 数
def estimate_request(
    system: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> int:
    return estimate_text(system) + estimate_messages(messages) + estimate_text(_to_text(tools))


# 把任意值转成用于估算的文本
def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# 把 tool_result 的 content 拍平成文本
def _flatten(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content)


@dataclass
class RequestProjector:
    """用上一次请求的真实 input_tokens 校准下一次请求的预估。

    纯字符估算对中文只有「数量级」精度；而 Anthropic 返回的 usage.input_tokens
    是精确值。所以有基准时用「上次真实值 + 新增消息估算」，比整段重估准得多。
    """

    _baseline_tokens: int = 0
    _baseline_count: int = 0

    # 记录一次真实请求的输入 token 数与发出时的消息条数
    def observe(self, input_tokens: int, message_count: int) -> None:
        if input_tokens > 0:
            self._baseline_tokens = input_tokens
            self._baseline_count = message_count

    # 历史被压缩或重建后，基准随即失效
    def reset(self) -> None:
        self._baseline_tokens = 0
        self._baseline_count = 0

    # 预估下一次请求的输入 token；没有基准时退回纯字符估算
    def project(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        if self._baseline_tokens > 0 and self._baseline_count <= len(messages):
            appended = messages[self._baseline_count:]
            return self._baseline_tokens + estimate_messages(appended)
        return estimate_request(system, messages, tools)
