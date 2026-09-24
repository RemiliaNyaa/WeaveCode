from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable

from weavecode.core.storage.database import Database

log = logging.getLogger(__name__)

# 一条迁移：版本号（从 1 递增）+ 名字 + 建表/改表的同步函数
Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]

# 迁移清单：**只能往后追加**，已发布的迁移不许改（否则老库跑不到新 schema）
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


# 执行所有未跑的迁移（逐条在事务里跑并记账），返回本次执行了哪些版本号
def apply(conn: sqlite3.Connection) -> list[int]:
    _ensure_table(conn)
    done = {row[0] for row in conn.execute("SELECT id FROM migration")}
    ran: list[int] = []
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
        ran.append(version)
        log.info("db: migration %d (%s) applied", version, name)
    return ran


# 在给定 Database 上跑迁移（daemon 启动时调一次）
async def apply_migrations(db: Database) -> list[int]:
    return await db.run(apply)
