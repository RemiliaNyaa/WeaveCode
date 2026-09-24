from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from weavecode.core.compact.compactor import (
    CHECKPOINT_ACK,
    Compactor,
    build_checkpoint,
    build_prompt,
    select,
    split_checkpoint,
)
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, UsageStats
from weavecode.core.storage import Database, apply_migrations


def _stub_provider(summary: str = "## 1. Original Goal\nTest\n## 2. Completed Steps\n- done") -> Any:
    provider = MagicMock()
    provider.chat = AsyncMock(return_value=LlmResponse(
        stop_reason="end_turn",
        text=summary,
        usage=UsageStats(input_tokens=100, output_tokens=30),
    ))
    return provider


def _make_messages(n: int = 5) -> list[dict[str, Any]]:
    msgs = []
    for i in range(n):
        msgs.append({"role": "user", "content": "user message " + "x" * 200})
        msgs.append({"role": "assistant", "content": "assistant reply " + "y" * 200})
    return msgs


# 功能：验证 compact_messages 成功时 provider.chat 被调用一次且不传工具 schema
# 设计：stub provider 返回非空摘要，断言 chat 调用一次，tool_schemas=[]
def test_compact_messages_calls_provider(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, None, "sess-1")
    messages = _make_messages()

    result = asyncio.get_event_loop().run_until_complete(
        compactor.compact_messages(messages, provider)
    )

    assert result is not None
    provider.chat.assert_called_once()
    call_kwargs = provider.chat.call_args
    assert call_kwargs.kwargs.get("tool_schemas") == [] or call_kwargs.args[1] == []


# 功能：验证 compact_messages 返回的摘要文本来自 provider 响应
# 设计：stub provider 返回固定摘要字符串，断言 result.summary_text 等于该字符串
def test_compact_messages_returns_summary(tmp_path: Path) -> None:
    expected = "## 1. Original Goal\nDo X\n## 2. Completed\n- step one"
    provider = _stub_provider(summary=expected)
    bus = EventBus()
    compactor = Compactor(bus, None, "sess-1")

    result = asyncio.get_event_loop().run_until_complete(
        compactor.compact_messages(_make_messages(), provider)
    )

    assert result is not None
    assert result.summary_text == expected


# 功能：验证 compact() 将 context.messages 替换为两条摘要消息对
# 设计：调用 compact() 后断言 messages 长度为 2，role 分别为 user/assistant
def test_compact_replaces_context_messages(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    compactor = Compactor(bus, None, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.get_event_loop().run_until_complete(compactor.compact(ctx, provider))

    assert len(ctx.messages) == 2
    assert ctx.messages[0]["role"] == "user"
    assert ctx.messages[1]["role"] == "assistant"


# 功能：验证 compact() 把摘要文本写进 summary 表
# 设计：用 tmp_path 建真实 db（先插 project + session 满足外键），调用 compact() 后查表断言摘要落库
async def test_compact_saves_summary(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)

    def _seed(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO project (id, worktree, name, time_created, time_updated) "
            "VALUES ('p1', '/proj', '', 0, 0)"
        )
        conn.execute(
            "INSERT INTO session (id, project_id, mode, status, title, directory, run_ids, "
            "time_created, time_updated) VALUES ('sess-1', 'p1', 'chat', 'active', '', "
            "'/proj', '[]', 0, 0)"
        )

    await db.run(_seed)

    provider = _stub_provider()
    compactor = Compactor(EventBus(), db, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    await compactor.compact(ctx, provider)

    def _read(conn: sqlite3.Connection) -> list[str]:
        rows = conn.execute(
            "SELECT text FROM summary WHERE session_id = ?", ("sess-1",)
        ).fetchall()
        return [str(r["text"]) for r in rows]

    texts = await db.run(_read)
    assert len(texts) == 1
    assert "## 1. Original Goal" in texts[0]


# 功能：验证 db 为 None 时压缩照常完成、只是不落库
# 设计：传 None 作 db 调用 compact()，断言不抛异常且 context 仍被替换 —— 无存储场景（CLI/单测）的降级路径
def test_compact_without_db_still_works(tmp_path: Path) -> None:
    provider = _stub_provider()
    compactor = Compactor(EventBus(), None, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.get_event_loop().run_until_complete(compactor.compact(ctx, provider))

    assert len(ctx.messages) == 2


# 功能：验证 compact() 成功后发布 ContextCompactedEvent 事件
# 设计：订阅 EventBus，收集事件，断言收到类型为 context.compacted 的事件
def test_compact_publishes_event(tmp_path: Path) -> None:
    provider = _stub_provider()
    bus = EventBus()
    received: list[Any] = []

    async def handler(event: Any) -> None:
        received.append(event)

    bus.subscribe(handler)
    compactor = Compactor(bus, None, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    ctx.messages = _make_messages()

    asyncio.get_event_loop().run_until_complete(compactor.compact(ctx, provider))

    types = [getattr(e, "type", None) for e in received]
    assert "context.compacted" in types


# 功能：验证 provider 抛异常时 context.messages 保持不变
# 设计：stub provider.chat 抛 RuntimeError，断言 compact() 返回 None 且 messages 未被修改
def test_compact_failure_preserves_context(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.chat = AsyncMock(side_effect=RuntimeError("LLM error"))
    bus = EventBus()
    compactor = Compactor(bus, None, "sess-1")
    ctx = ExecutionContext(run_id="r1", goal="test", max_steps=5)
    original_messages = _make_messages()
    ctx.messages = list(original_messages)

    result = asyncio.get_event_loop().run_until_complete(compactor.compact(ctx, provider))

    assert result is None
    assert ctx.messages == original_messages


# 功能：验证 checkpoint 文本可被 round-trip 解析回摘要与最近原文
# 设计：先用 build_checkpoint 生成，再用 split_checkpoint 取回，断言两段内容一致
def test_split_checkpoint_roundtrip() -> None:
    checkpoint = build_checkpoint("## 1. Original Goal\nGoal", "[USER]\nrecent body")
    messages = [
        {"role": "user", "content": checkpoint},
        {"role": "assistant", "content": CHECKPOINT_ACK},
        {"role": "user", "content": "new question"},
    ]

    prior, rest = split_checkpoint(messages)

    assert prior is not None
    assert prior["summary"] == "## 1. Original Goal\nGoal"
    assert "recent body" in prior["recent"]
    assert rest == [{"role": "user", "content": "new question"}]


# 功能：验证未压缩的普通历史不会被误判为 checkpoint
# 设计：普通 user 消息，断言返回 (None, 原列表)，保证首次压缩走首次分支
def test_split_checkpoint_ignores_normal_history() -> None:
    messages = [{"role": "user", "content": "hello"}]

    prior, rest = split_checkpoint(messages)

    assert prior is None
    assert rest == messages


# 功能：验证 tool_result（content 为 block 列表）不会被误认为 checkpoint
# 设计：即使 block 文本里出现 <conversation-checkpoint> 标记也不该命中——靠类型区分
def test_split_checkpoint_ignores_tool_result_with_marker() -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "<conversation-checkpoint><summary>x</summary>",
                }
            ],
        }
    ]

    prior, _ = split_checkpoint(messages)

    assert prior is None


# 功能：验证 select 从最新往前切出不超过 keep_tokens 的 recent
# 设计：每条消息约 104 token，keep=250，断言保留最后 2 条、其余进 head
def test_select_keeps_recent_within_budget() -> None:
    messages = [{"role": "user", "content": "x" * 400} for _ in range(5)]

    head, recent = select(messages, keep_tokens=250)

    assert len(recent) == 2
    assert len(head) == 3


# 功能：验证 keep_tokens 为 0 时所有消息都进 head（等价于全量压缩）
# 设计：边界值，断言 recent 为空且 head 等于原列表
def test_select_zero_keep_puts_everything_in_head() -> None:
    messages = [{"role": "user", "content": "x"}]

    head, recent = select(messages, keep_tokens=0)

    assert recent == []
    assert head == messages


# 功能：验证首次压缩的提示词只有 <conversation>，不含 <prior-summary>
# 设计：prior_summary=None，断言首次指令存在、合并指令与 prior 块都不存在
def test_build_prompt_first_time() -> None:
    prompt = build_prompt("body", None)

    assert "<conversation>" in prompt
    assert "Create a new anchored summary" in prompt
    assert "<prior-summary>" not in prompt


# 功能：验证二次压缩会注入 <prior-summary> 块与合并指令
# 设计：传入旧摘要，断言旧摘要被单独包裹、且合并告警语出现
def test_build_prompt_merge() -> None:
    prompt = build_prompt("body", "old summary")

    assert "<prior-summary>\nold summary\n</prior-summary>" in prompt
    assert "is discarded after this" in prompt
    assert "Create a new anchored summary" not in prompt


# 功能：验证对已含 checkpoint 的历史再次压缩时走合并路径
# 设计：stub provider 捕获实际提示词，断言含 <prior-summary> 与旧摘要原文
async def test_second_compaction_merges_prior_summary(tmp_path: Path) -> None:
    provider = _stub_provider()
    compactor = Compactor(EventBus(), tmp_path, "sess-1")
    messages = [
        {"role": "user", "content": build_checkpoint("OLD SUMMARY", "old recent")},
        {"role": "assistant", "content": CHECKPOINT_ACK},
        {"role": "user", "content": "next task " + "x" * 400},
        {"role": "assistant", "content": "ok " + "y" * 400},
    ]

    result = await compactor.compact_messages(messages, provider)

    prompt = provider.chat.call_args.kwargs["messages"][0]["content"]
    assert "<prior-summary>\nOLD SUMMARY\n</prior-summary>" in prompt
    assert "old recent" in prompt
    assert result is not None


# 功能：验证压缩结果替换历史后是 [checkpoint, 确认] 两条合法消息
# 设计：断言首条为 user 且以 <conversation-checkpoint> 开头，次条为 assistant 确认
def test_result_as_messages_shape(tmp_path: Path) -> None:
    provider = _stub_provider()
    compactor = Compactor(EventBus(), tmp_path, "sess-1")
    messages = _make_messages()

    result = asyncio.get_event_loop().run_until_complete(
        compactor.compact_messages(messages, provider)
    )

    assert result is not None
    rendered = result.as_messages()
    assert len(rendered) == 2
    assert rendered[0]["role"] == "user"
    assert str(rendered[0]["content"]).startswith("<conversation-checkpoint>")
    assert rendered[1] == {"role": "assistant", "content": CHECKPOINT_ACK}


# 功能：验证 recent 窗口内的消息以原文形式保留在 checkpoint 里
# 设计：keep_tokens 设为很大，断言最后一条历史原文出现在渲染结果中
def test_recent_text_preserved_in_checkpoint(tmp_path: Path) -> None:
    provider = _stub_provider()
    compactor = Compactor(EventBus(), tmp_path, "sess-1", keep_tokens=1_000_000)
    messages = [
        {"role": "user", "content": "MARKER-UNIQUE-12345"},
        {"role": "assistant", "content": "ack"},
    ]

    result = asyncio.get_event_loop().run_until_complete(
        compactor.compact_messages(messages, provider)
    )

    assert result is not None
    assert "MARKER-UNIQUE-12345" in result.as_messages()[0]["content"]
