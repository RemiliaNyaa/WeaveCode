from __future__ import annotations

import sqlite3
import time

from weavecode.core.storage.database import Database

# 持久化规则：(project, permission, resource) 三元组
Rule = tuple[str, str, str]


# 从 permission 表读出全部规则（排序只为结果稳定，便于测试与日志）
async def load_policy(db: Database) -> list[Rule]:
    def _read(conn: sqlite3.Connection) -> list[Rule]:
        rows = conn.execute(
            "SELECT project, permission, resource FROM permission "
            "ORDER BY project, permission, resource"
        ).fetchall()
        return [(str(r["project"]), str(r["permission"]), str(r["resource"])) for r in rows]

    return await db.run(_read)


# 把一条规则写进 permission 表；同一三元组重复写不产生新行（靠唯一索引 + DO NOTHING 保证幂等）
async def save_rule(db: Database, rule: Rule) -> None:
    project, permission, resource = rule
    now = int(time.time() * 1000)

    def _write(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO permission (project, permission, resource, time_created) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(project, permission, resource) DO NOTHING",
            (project, permission, resource, now),
        )

    await db.transaction(_write)
