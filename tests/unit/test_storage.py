from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from weavecode.core.storage.database import DEFAULT_DB_PATH, Database


# 造一个指向临时文件的 Database
def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


# 功能：默认数据库路径是全局单文件 ~/.weave/weave.db
# 设计：直接断言常量——它是「全局单文件」这条设计决策的落点，不该被随手改掉
def test_default_path_is_global_single_file() -> None:
    assert DEFAULT_DB_PATH == Path("~/.weave/weave.db")


# 功能：打开数据库后 WAL 与外键约束真的生效
# 设计：查 PRAGMA 实际值——「WAL + 外键」是并发与引用完整性的前提，不能只写在注释里
@pytest.mark.asyncio
async def test_pragmas_applied(tmp_path: Path) -> None:
    db = _db(tmp_path)

    journal = await db.run(lambda c: c.execute("PRAGMA journal_mode").fetchone()[0])
    foreign_keys = await db.run(lambda c: c.execute("PRAGMA foreign_keys").fetchone()[0])

    assert journal == "wal"
    assert foreign_keys == 1


# 功能：run 把同步逻辑的返回值原样带回，且重复调用复用同一个连接
# 设计：用「建表 → 插入 → 查回」走一遍真实读写；两次 run 能看到同一张表，即证明连接被复用
@pytest.mark.asyncio
async def test_run_executes_and_reuses_connection(tmp_path: Path) -> None:
    db = _db(tmp_path)

    await db.run(lambda c: c.execute("CREATE TABLE t (v TEXT)"))
    await db.run(lambda c: c.execute("INSERT INTO t VALUES ('x')"))
    rows = await db.run(lambda c: c.execute("SELECT v FROM t").fetchall())

    assert [r[0] for r in rows] == ["x"]


# 功能：transaction 提交后数据可见
# 设计：正常路径——断言事务内的写入在事务外查得到
@pytest.mark.asyncio
async def test_transaction_commits(tmp_path: Path) -> None:
    db = _db(tmp_path)
    await db.run(lambda c: c.execute("CREATE TABLE t (v TEXT)"))

    await db.transaction(lambda c: c.execute("INSERT INTO t VALUES ('y')"))

    rows = await db.run(lambda c: c.execute("SELECT v FROM t").fetchall())
    assert [r[0] for r in rows] == ["y"]


# 功能：transaction 抛异常时整段回滚，不留半截数据
# 设计：事务内先插一行再抛——断言表里查不到那行，这是「原子性」的直接证据
@pytest.mark.asyncio
async def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    db = _db(tmp_path)
    await db.run(lambda c: c.execute("CREATE TABLE t (v TEXT)"))

    def boom(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO t VALUES ('z')")
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await db.transaction(boom)

    rows = await db.run(lambda c: c.execute("SELECT v FROM t").fetchall())
    assert rows == []


# 功能：并发调用 run 会被串行化（不会两个线程同时用一个连接）
# 设计：每个 action 睡 50ms 并记录自己占用连接的时间区间；断言区间两两不重叠——
#       锁失效时区间会交错，这条用例必然失败
@pytest.mark.asyncio
async def test_run_serializes_concurrent_calls(tmp_path: Path) -> None:
    db = _db(tmp_path)
    spans: list[tuple[float, float]] = []

    def action(conn: sqlite3.Connection) -> None:
        start = time.monotonic()
        time.sleep(0.05)
        spans.append((start, time.monotonic()))

    await asyncio.gather(*[db.run(action) for _ in range(3)])

    spans.sort()
    for (_, end), (next_start, _) in zip(spans, spans[1:]):
        assert end <= next_start, "两次 run 的占用区间重叠了 —— 锁没起作用"
