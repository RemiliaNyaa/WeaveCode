from __future__ import annotations

from typing import Any

from weavecode.core.compact.tokens import estimate_messages

TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000


# 确定性降级：从最老的消息开始丢弃，直到估算不超过预算；不调 LLM、不会失败
def shrink_to_fit(messages: list[dict[str, Any]], budget_tokens: int) -> list[dict[str, Any]]:
    kept = list(messages)
    while len(kept) > 1 and estimate_messages(kept) > budget_tokens:
        kept = kept[1:]
    # 丢弃头部可能让首条变成「没有对应 tool_use 的 tool_result」，继续丢掉直到合法
    while len(kept) > 1 and _leads_with_tool_result(kept[0]):
        kept = kept[1:]
    return kept


# 判断一条消息是否为只含 tool_result 的 user 消息
def _leads_with_tool_result(message: dict[str, Any]) -> bool:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return False
    return any(isinstance(block, dict) and block.get("type") == "tool_result" for block in content)


# 对消息列表中超长的 tool_result 内容做内存截断，返回处理后的新列表
def truncate_tool_results(
    messages: list[dict[str, Any]],
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
) -> list[dict[str, Any]]:
    result = []
    for msg in messages:
        if msg.get("role") != "user":
            result.append(msg)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue
        new_blocks = []
        for block in content:
            if block.get("type") == "tool_result" and isinstance(block.get("content"), str):
                text = block["content"]
                if len(text) > limit:
                    omitted = len(text) - keep
                    block = dict(block)
                    block["content"] = (
                        text[:keep]
                        + f"\n[... {omitted} chars omitted. Full output in run events.]"
                    )
            new_blocks.append(block)
        result.append({**msg, "content": new_blocks})
    return result
