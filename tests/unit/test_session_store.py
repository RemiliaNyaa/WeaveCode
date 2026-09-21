from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore
from weavecode.core.storage import Database, apply_migrations


# 造一个建好表、指向临时目录的 store
async def _store(tmp_path: Path) -> SessionStore:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)
    return SessionStore(db)


# 造一个最小 Session（working_dir 固定为 cwd，保证 project upsert 稳定）
def _session(sid: str = "sess-1", **overrides: Any) -> Session:
    base: dict[str, Any] = {
        "id": sid,
        "mode": "chat",
        "status": "waiting_for_input",
        "title": "hello",
        "created_at": "2026-09-21T00:00:00+00:00",
        "updated_at": "2026-09-21T00:01:00+00:00",
        "run_ids": ["run-1"],
        "working_dir": str(Path.cwd()),
    }
    base.update(overrides)
    return Session(**base)



# 功能：session meta 写入后能完整读回
# 设计：含 run_ids 的 Session 走一遍 DB 往返，断言字段全等——覆盖 session 表 + project upsert 的持久化契约
@pytest.mark.asyncio
async def test_meta_roundtrip(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    session = _session()

    await store.write_meta(session)
    loaded = await store.read_meta("sess-1")

    assert loaded == session


# 功能：同一 session 重复写 meta 是更新而不是报错，且 run_ids 会累积
# 设计：连写两次（第二次追加 run_id 并改状态），断言读回最新值——覆盖 ON CONFLICT DO UPDATE 这条路径
@pytest.mark.asyncio
async def test_meta_update_accumulates_runs(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.write_meta(_session())

    await store.write_meta(_session(run_ids=["run-1", "run-2"], status="active"))

    loaded = await store.read_meta("sess-1")
    assert loaded.run_ids == ["run-1", "run-2"]
    assert loaded.status == "active"


# 功能：读不存在的 session 抛 FileNotFoundError
# 设计：保持旧行为（原来读不到 meta.json 也抛），调用方靠它判断会话不存在
@pytest.mark.asyncio
async def test_read_missing_meta_raises(tmp_path: Path) -> None:
    store = await _store(tmp_path)

    with pytest.raises(FileNotFoundError):
        await store.read_meta("nope")


# 功能：含 tool_use / tool_result block 的 thread 能按 Anthropic 格式读回（list 形态的 content）
# 设计：追加 assistant tool_use 与 user tool_result，断言读回的就是原始 block 列表——
#       这是 message + part 拆表后「一个 block 一行」的往返证据
@pytest.mark.asyncio
async def test_thread_roundtrip_with_tool_blocks(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.write_meta(_session())
    await store.append_message("sess-1", "user", "read file")
    await store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "x"}}],
        run_id="run-1",
    )
    await store.append_message(
        "sess-1",
        "user",
        [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}],
        run_id="run-1",
    )

    messages = await store.read_messages("sess-1")

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
@pytest.mark.asyncio
async def test_read_messages_trims_orphan_tool_use_tail(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.write_meta(_session())
    await store.append_message("sess-1", "user", "hello")
    await store.append_message(
        "sess-1",
        "assistant",
        [{"type": "tool_use", "id": "orphan", "name": "read_file", "input": {}}],
        run_id="run-1",
    )

    assert await store.read_messages("sess-1") == [{"role": "user", "content": "hello"}]


# 功能：write_compacted 用压缩后的消息整体替换 thread（旧消息不再出现）
# 设计：先写两条旧消息、再写一条压缩结果，断言只剩那一条——覆盖「先 DELETE 再重插」这条路径
@pytest.mark.asyncio
async def test_write_compacted_replaces_thread(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await store.write_meta(_session())
    await store.append_message("sess-1", "user", "old-1")
    await store.append_message("sess-1", "assistant", "old-2")

    await store.write_compacted("sess-1", [{"role": "user", "content": "summary"}])

    assert await store.read_messages("sess-1") == [{"role": "user", "content": "summary"}]


# 功能：删掉 project 会级联清掉它下面的 session / message / part
# 设计：这是「按项目隔离靠外键」的直接证据——删项目即删它名下全部数据，不需要逐表手工清理
@pytest.mark.asyncio
async def test_deleting_project_cascades(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)
    store = SessionStore(db)
    await store.write_meta(_session())
    await store.append_message("sess-1", "user", "hi")

    await db.run(lambda c: c.execute("DELETE FROM project"))

    async def count(table: str) -> int:
        return await db.run(lambda c: c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    assert await count("session") == 0
    assert await count("message") == 0
    assert await count("part") == 0
