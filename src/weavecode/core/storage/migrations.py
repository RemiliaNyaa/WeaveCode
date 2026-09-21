from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable

from weavecode.core.storage.database import Database

# 一条迁移：版本号（从 1 递增）+ 名字 + 建表/改表的同步函数
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

# 迁移清单：编号从 1 起递增，新的迁移实现追加在本文件末尾
MIGRATIONS: list[Migration] = []


# 建迁移记账表（对齐 opencode 的 migration 表）
def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS migration ("
        "  id INTEGER PRIMARY KEY,"
        "  name TEXT NOT NULL,"
        "  time_completed INTEGER NOT NULL"
        ")"
    )


# 执行所有未跑的迁移（逐条在事务里跑并记账）
def apply(conn: sqlite3.Connection) -> None:
    _ensure_table(conn)
    done = {row[0] for row in conn.execute("SELECT id FROM migration")}
    for version, name, run in MIGRATIONS:
        if version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            run(conn)
            conn.execute(
                "INSERT INTO migration (id, name, time_completed) VALUES (?, ?, ?)",
                (version, name, int(time.time() * 1000)),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# 在给定 Database 上跑迁移（daemon 启动时调一次）
async def apply_migrations(db: Database) -> None:
    await db.run(apply)
