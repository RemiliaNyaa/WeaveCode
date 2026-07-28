from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore


# 造一个指向临时目录的 store
def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path)


# 造一个最小 Session
def _session(sid: str = "sess-1", **overrides: Any) -> Session:
    base: dict[str, Any] = {
        "id": sid,
        "mode": "chat",
        "status": "waiting_for_input",
        "title": "hello",
        "created_at": "2026-07-27T00:00:00+00:00",
        "updated_at": "2026-07-27T00:01:00+00:00",
        "run_ids": ["run-1"],
    }
    base.update(overrides)
    return Session(**base)



# 功能：session meta 写入后能完整读回
# 设计：含 run_ids 的 Session 走一遍存储往返，断言字段全等——覆盖 meta 持久化契约
def test_meta_roundtrip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    session = _session()

    store.write_meta(session)
    loaded = store.read_meta("sess-1")

    assert loaded == session


# 功能：同一 session 重复写 meta 是更新而不是报错，且 run_ids 会累积
# 设计：连写两次（第二次追加 run_id 并改状态），断言读回最新值
def test_meta_update_accumulates_runs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_meta(_session())

    store.write_meta(_session(run_ids=["run-1", "run-2"], status="active"))

    loaded = store.read_meta("sess-1")
    assert loaded.run_ids == ["run-1", "run-2"]
    assert loaded.status == "active"


# 功能：读不存在的 session 抛 FileNotFoundError
# 设计：保持旧行为（原来读不到 meta.json 也抛），调用方靠它判断会话不存在
def test_read_missing_meta_raises(tmp_path: Path) -> None:
    store = _store(tmp_path)

    with pytest.raises(FileNotFoundError):
        store.read_meta("nope")


# 功能：含 tool_use / tool_result block 的 thread 能按 Anthropic 格式读回（list 形态的 content）
# 设计：追加 assistant tool_use 与 user tool_result，断言读回的就是原始 block 列表
def test_thread_roundtrip_with_tool_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_meta(_session())
    store.append_message("sess-1", "user", "read file")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}],
        run_id="run-1",
    )
    store.append_message(
        "sess-1",
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        run_id="run-1",
    )

    messages = store.read_messages("sess-1")

    assert messages == [
        {"role": "user", "content": "read file"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        },
    ]


# 功能：尾部未配对的 tool_use 会被裁掉
# 设计：构造一条没有 tool_result 的 assistant tool_use，断言只返回配平之前的内容，避免 API 报 messages.invalid
def test_read_messages_trims_orphan_tool_use_tail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_meta(_session())
    store.append_message("sess-1", "user", "hello")
    store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}}],
        run_id="run-1",
    )

    assert store.read_messages("sess-1") == [{"role": "user", "content": "hello"}]


# 功能：write_compacted 用压缩后的消息整体替换 thread（旧消息不再出现）
# 设计：先写两条旧消息、再写一条压缩结果，断言只剩那一条——覆盖「先 DELETE 再重插」这条路径
def test_write_compacted_replaces_thread(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.write_meta(_session())
    store.append_message("sess-1", "user", "old-1")
    store.append_message("sess-1", "assistant", "old-2")

    store.write_compacted("sess-1", [{"role": "user", "content": "summary"}])

    assert store.read_messages("sess-1") == [{"role": "user", "content": "summary"}]