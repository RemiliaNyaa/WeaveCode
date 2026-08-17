from __future__ import annotations

from typing import Any

TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000

# 本次请求要给输出预留的 token（对应硬编码的 max_tokens）
_RESERVE_TOKENS = 8_192


# 按上一轮请求的上下文水位换算本轮输入可用的预算：
# 窗口里已被占用的部分扣掉，再为本次输出留出余量；水位打满时预算为 0
def budget_from_watermark(
    context_pct: float,
    window_tokens: int,
    reserve_tokens: int = _RESERVE_TOKENS,
) -> int:
    used = int(context_pct * window_tokens)
    return max(0, window_tokens - used - reserve_tokens)


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
