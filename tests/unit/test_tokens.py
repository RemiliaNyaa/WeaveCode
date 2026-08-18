from __future__ import annotations

from weavecode.core.compact.tokens import (
    estimate_messages,
    estimate_text,
)


# 功能：验证纯 ASCII 文本按 4 字符约 1 token 估算
# 设计：取 400 个 ASCII 字符，断言估算恰为 100，锁定 chars/4 分支
def test_estimate_text_ascii() -> None:
    assert estimate_text("a" * 400) == 100


# 功能：验证混排文本整体按字符数估算
# 设计：50 汉字 + 200 ASCII 共 250 字符，按 chars/4 向上取整得 63
def test_estimate_text_mixed() -> None:
    assert estimate_text("中" * 50 + "a" * 200) == 63


# 功能：验证空字符串估算为 0
# 设计：边界值，避免出现负数或异常
def test_estimate_text_empty() -> None:
    assert estimate_text("") == 0


# 功能：验证消息估算会把 tool_result 等 block 计入，而不是只看纯文本
# 设计：tool_result 里放 50 个汉字，断言总量不小于 50，证明 block 内容被统计
def test_estimate_messages_counts_tool_blocks() -> None:
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "/x"}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "中" * 50},
            ],
        },
    ]
    assert estimate_messages(messages) >= 50
