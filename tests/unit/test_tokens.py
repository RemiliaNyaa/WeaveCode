from __future__ import annotations

from weavecode.core.compact.tokens import (
    RequestProjector,
    estimate_messages,
    estimate_request,
    estimate_text,
)


# 功能：验证纯 ASCII 文本按 4 字符约 1 token 估算
# 设计：取 400 个 ASCII 字符，断言估算恰为 100，锁定 chars/4 分支
def test_estimate_text_ascii() -> None:
    assert estimate_text("a" * 400) == 100


# 功能：验证汉字按 1 字符约 1 token 估算，不被 ASCII 公式低估 4 倍
# 设计：取 100 个汉字断言估算为 100——这正是相对 opencode 的 chars/4 的核心改进
def test_estimate_text_cjk() -> None:
    assert estimate_text("中" * 100) == 100


# 功能：验证中英混排时两部分各按自己比例累加
# 设计：50 汉字 + 200 ASCII，期望 50 + 50 = 100
def test_estimate_text_mixed() -> None:
    assert estimate_text("中" * 50 + "a" * 200) == 100


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


# 功能：验证没有真实基准时 project 退回纯字符估算
# 设计：不调用 observe，断言 project 与 estimate_request 完全一致
def test_projector_without_baseline_uses_estimate() -> None:
    projector = RequestProjector()
    messages = [{"role": "user", "content": "hello"}]
    assert projector.project("sys", messages, []) == estimate_request("sys", messages, [])


# 功能：验证有真实基准时以真实 input_tokens 为基数，而不是整段重估
# 设计：observe 一个远大于字符估算的真实值，断言结果不小于该真实值
def test_projector_uses_real_usage_baseline() -> None:
    projector = RequestProjector()
    messages = [{"role": "user", "content": "hello"}]
    projector.observe(12_345, 1)
    assert projector.project("sys", messages, []) >= 12_345


# 功能：验证历史被压缩（消息变少）后基准自动失效，退回字符估算
# 设计：observe 记录 5 条消息，随后只传 1 条，断言不会沿用旧基准；reset 后同理
def test_projector_invalidates_when_history_shrinks() -> None:
    projector = RequestProjector()
    projector.observe(50_000, 5)
    messages = [{"role": "user", "content": "hi"}]
    assert projector.project("s", messages, []) == estimate_request("s", messages, [])
    projector.reset()
    assert projector.project("s", messages, []) == estimate_request("s", messages, [])


# 功能：验证基准只在消息增长时叠加新增部分
# 设计：observe(1000, 1) 后追加一条消息，断言结果 = 1000 + 新增消息估算
def test_projector_adds_appended_messages() -> None:
    projector = RequestProjector()
    first = {"role": "user", "content": "x"}
    appended = {"role": "assistant", "content": "y" * 400}
    projector.observe(1_000, 1)
    projected = projector.project("s", [first, appended], [])
    assert projected == 1_000 + estimate_messages([appended])
