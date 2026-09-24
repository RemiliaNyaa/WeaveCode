from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from weavecode.core.bus.events import RunFinishedEvent, RunStartedEvent
from weavecode.core.events.bus import EventBus
from weavecode.core.events.writer import EventWriter, read_events
from weavecode.core.storage import Database, apply_migrations


# 造一个建好表、指向临时目录的 db（测试用）
async def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)
    return db


# 读出某 run 的 (seq, type) 列表，按 seq 升序
async def _rows(db: Database, run_id: str) -> list[tuple[int, str]]:
    def _read(conn: sqlite3.Connection) -> list[tuple[int, str]]:
        rows = conn.execute(
            "SELECT seq, type FROM event WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [(int(r["seq"]), str(r["type"])) for r in rows]

    return await db.run(_read)


# 功能：验证 handle 后事件被写进 event 表（type 与 data 两列都对）
# 设计：用真实 SQLite（tmp_path）而非 mock，因为 EventWriter 的核心职责是落库，只有真查出来才能证明写入正确
async def test_event_writer_writes_event_row(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    event = RunStartedEvent(run_id="run-1", goal="test goal", ts="2026-05-11T00:00:00Z")

    async with EventWriter(db, "run-1") as writer:
        await writer.handle(event)

    assert await _rows(db, "run-1") == [(1, "run.started")]
    stored = (await read_events(db, "run-1"))[0]
    assert stored["run_id"] == "run-1"
    assert stored["goal"] == "test goal"


# 功能：验证同一 run 内多次 handle 的 seq 从 1 连续递增
# 设计：写三条事件后断言 (seq, type) 序列 —— 覆盖「JSONL 靠行序、DB 必须显式存序号」这一迁移关键点
async def test_event_writer_seq_increases_from_one(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    async with EventWriter(db, "r1") as writer:
        await writer.handle(RunStartedEvent(run_id="r1", goal="g1", ts="2026-05-11T00:00:00Z"))
        await writer.handle(RunStartedEvent(run_id="r1", goal="g2", ts="2026-05-11T00:00:00Z"))
        finished = RunFinishedEvent(
            run_id="r1", status="success", steps=2, ts="2026-05-11T00:00:01Z"
        )
        await writer.handle(finished)

    assert await _rows(db, "r1") == [
        (1, "run.started"),
        (2, "run.started"),
        (3, "run.finished"),
    ]


# 功能：验证两个 writer 绑同一 run_id 时序号仍连续、不撞号
# 设计：顺序使用两个 writer（模拟父 run 与子 run 事件交织），断言序号无重复无空洞 —— 这正是 event_sequence 表存在的理由
async def test_event_writer_two_writers_same_run_do_not_clash(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    async with EventWriter(db, "r1") as w1:
        await w1.handle(event)
        await w1.handle(event)
    async with EventWriter(db, "r1") as w2:
        await w2.handle(event)

    assert await _rows(db, "r1") == [
        (1, "run.started"),
        (2, "run.started"),
        (3, "run.started"),
    ]


# 功能：验证不同 run_id 的序号各自从 1 开始、互不影响
# 设计：往两个 run 各写事件，断言各自 seq 独立 —— 序号是「run 内序号」而非全局序号，回放才能按 run 隔离
async def test_event_writer_seq_is_per_run(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    event = RunStartedEvent(run_id="x", goal="g", ts="2026-05-11T00:00:00Z")

    async with EventWriter(db, "a") as wa:
        await wa.handle(event)
    async with EventWriter(db, "b") as wb:
        await wb.handle(event)

    assert await _rows(db, "a") == [(1, "run.started")]
    assert await _rows(db, "b") == [(1, "run.started")]


# 功能：验证 subscribe 把 writer 接入 EventBus 后，bus.publish 能触发落库
# 设计：通过 bus.publish 触发写入（而非直接调 writer.handle），测试集成路径，确认订阅接线正确
async def test_event_writer_subscribe_via_bus(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    bus = EventBus()
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    async with EventWriter(db, "r1") as writer:
        writer.subscribe(bus)
        await bus.publish(event)

    assert await _rows(db, "r1") == [(1, "run.started")]


# 功能：验证未通过 async with 打开时 handle 静默返回、不落库也不抛异常
# 设计：直接实例化 writer（跳过 async with），以「不引发异常且表内无记录」为判据；对应 EventWriter 的防御性设计
async def test_event_writer_handle_when_not_open_is_noop(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    writer = EventWriter(db, "r1")
    await writer.handle(RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z"))
    assert await _rows(db, "r1") == []


# 功能：验证 db 为 None（未配置存储）时 handle 静默跳过，agent 不因缺少存储而失败
# 设计：传 None 作 db，断言不抛异常 —— 对应 runner 在无 db 场景（CLI / 单测）下的降级路径
async def test_event_writer_without_db_is_noop() -> None:
    writer = EventWriter(None, "r1")
    async with writer:
        await writer.handle(RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z"))


# 功能：验证落库失败时只记录 ERROR 日志、不向上传播异常
# 设计：monkeypatch 让 db.transaction 抛 sqlite3.Error，覆盖「落库失败不终止 agent」这一契约（真实触发需破坏 DB 文件，不值得）
async def test_event_writer_db_error_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = await _db(tmp_path)

    async def _boom(_action: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "transaction", _boom)
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    with caplog.at_level(logging.ERROR, logger="weavecode.core.events.writer"):
        async with EventWriter(db, "r1") as writer:
            await writer.handle(event)

    assert any("failed to write" in r.message for r in caplog.records)


# 功能：验证 read_events 按 seq 顺序返回事件（而非插入顺序或全局自增 id 顺序）
# 设计：手工插两条 seq 颠倒的记录，断言读出顺序跟着 seq 走 —— 回放顺序不能依赖跨 run 混用的自增 id
async def test_read_events_orders_by_seq(tmp_path: Path) -> None:
    db = await _db(tmp_path)

    def _seed(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO event_sequence (run_id, seq) VALUES (?, ?)", ("r1", 2))
        for seq in (2, 1):
            conn.execute(
                "INSERT INTO event (run_id, seq, type, data, time_created) "
                "VALUES (?, ?, ?, ?, ?)",
                ("r1", seq, f"t{seq}", json.dumps({"n": seq}), 0),
            )

    await db.run(_seed)
    assert [e["n"] for e in await read_events(db, "r1")] == [1, 2]
