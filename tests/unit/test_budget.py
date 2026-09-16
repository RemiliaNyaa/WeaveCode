from __future__ import annotations

from weavecode.core.compact.budget import shrink_to_fit, truncate_tool_results


def _make_tool_result_msg(content: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "id1", "content": content}],
    }


# 功能：验证 tool_result 内容未超过阈值时原文不变
# 设计：构造 7999 字符内容（刚好低于 8000），断言消息原样返回
def test_short_tool_result_untouched() -> None:
    text = "x" * 7999
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 tool_result 内容超过阈值时被截断并附加省略标记
# 设计：构造 10000 字符内容，断言截断后长度 < 原始，且包含省略标记字符串
def test_long_tool_result_truncated() -> None:
    text = "y" * 10_000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    truncated = result[0]["content"][0]["content"]
    assert len(truncated) < len(text)
    assert "chars omitted" in truncated
    assert truncated.startswith("y" * 4000)


# 功能：验证 tool_result 内容恰好等于阈值时不截断
# 设计：构造恰好 8000 字符内容，断言原文保持不变
def test_exact_limit_untouched() -> None:
    text = "z" * 8000
    msgs = [_make_tool_result_msg(text)]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["content"] == text


# 功能：验证 text 类型 block 不受截断影响
# 设计：构造含 text block 的 user 消息，内容超过阈值，断言内容原样返回
def test_non_tool_result_block_untouched() -> None:
    long_text = "a" * 20_000
    msgs = [{"role": "user", "content": [{"type": "text", "text": long_text}]}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"][0]["text"] == long_text


# 功能：验证同一 user 消息含多个 tool_result 时各自独立判断截断
# 设计：构造一条消息含两个 tool_result，一短一长，断言只有长的被截断
def test_multiple_tool_results_independent() -> None:
    short = "s" * 100
    long = "l" * 10_000
    msgs = [{
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "a", "content": short},
            {"type": "tool_result", "tool_use_id": "b", "content": long},
        ],
    }]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    blocks = result[0]["content"]
    assert blocks[0]["content"] == short
    assert "chars omitted" in blocks[1]["content"]


# 功能：验证 assistant 消息不被截断处理
# 设计：构造超长内容的 assistant 消息，断言原样返回
def test_assistant_message_untouched() -> None:
    text = "a" * 20_000
    msgs = [{"role": "assistant", "content": text}]
    result = truncate_tool_results(msgs, limit=8000, keep=4000)
    assert result[0]["content"] == text


# 功能：验证确定性降级从最老的消息开始丢弃，直到估算不超过预算
# 设计：10 条各约 104 token 的消息、预算 300，断言只剩 2 条
def test_shrink_drops_oldest_until_fits() -> None:
    messages = [{"role": "user", "content": "x" * 400} for _ in range(10)]

    kept = shrink_to_fit(messages, budget_tokens=300)

    assert len(kept) == 2
    assert kept == messages[-2:]


# 功能：验证预算充足时不丢弃任何消息
# 设计：单条短消息 + 大预算，断言返回内容与输入一致
def test_shrink_noop_when_within_budget() -> None:
    messages = [{"role": "user", "content": "hi"}]

    assert shrink_to_fit(messages, budget_tokens=10_000) == messages


# 功能：验证丢弃头部后若首条成了孤立 tool_result，会继续丢弃直到合法
# 设计：构造 assistant / tool_result / assistant，预算刚好要求丢掉第一条，
#       断言结果首条不再是只含 tool_result 的 user 消息
def test_shrink_skips_orphan_tool_result() -> None:
    messages = [
        {"role": "assistant", "content": "x" * 4_000},
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "y"}],
        },
        {"role": "assistant", "content": "z"},
    ]

    kept = shrink_to_fit(messages, budget_tokens=200)

    assert kept
    first = kept[0]
    assert not (first["role"] == "user" and isinstance(first["content"], list))


# 功能：验证只剩一条消息时不做无限丢弃（保底不返回空列表）
# 设计：单条超大消息 + 极小预算，断言结果仍保留这条消息
def test_shrink_keeps_last_message() -> None:
    messages = [{"role": "user", "content": "x" * 100_000}]

    kept = shrink_to_fit(messages, budget_tokens=10)

    assert len(kept) == 1
