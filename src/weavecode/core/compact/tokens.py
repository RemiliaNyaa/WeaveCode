from __future__ import annotations

import json
from typing import Any

# 4 个字符约等于 1 token 的粗略比例
_CHARS_PER_TOKEN = 4


# 估算文本 token 数：一律按「字符数 ÷ 4」向上取整
def estimate_text(text: str) -> int:
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


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
