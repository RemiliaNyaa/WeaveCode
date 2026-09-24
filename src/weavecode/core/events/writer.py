from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from pydantic import BaseModel

from weavecode.core.events.bus import EventBus
from weavecode.core.storage.database import Database

logger = logging.getLogger(__name__)


# 取事件的类型名（bus 事件都带 type 字段；没有该字段时退回类名）
def _event_type(event: BaseModel) -> str:
    value = getattr(event, "type", None)
    return value if isinstance(value, str) else type(event).__name__


# 把一条事件写进 event 表：先在 event_sequence 里取号，再插 event —— 两步必须在同一事务里
def _append_event(conn: sqlite3.Connection, run_id: str, event: BaseModel) -> None:
    row = conn.execute("SELECT seq FROM event_sequence WHERE run_id = ?", (run_id,)).fetchone()
    seq = int(row["seq"]) + 1 if row is not None else 1
    conn.execute(
        "INSERT INTO event_sequence (run_id, seq) VALUES (?, ?) "
        "ON CONFLICT(run_id) DO UPDATE SET seq = excluded.seq",
        (run_id, seq),
    )
    conn.execute(
        "INSERT INTO event (run_id, seq, type, data, time_created) VALUES (?, ?, ?, ?, ?)",
        (run_id, seq, _event_type(event), event.model_dump_json(), int(time.time() * 1000)),
    )


# 按 seq 顺序读回某 run 的全部事件（回放用）；顺序必须靠 seq，不能靠全局自增的 id
async def read_events(db: Database, run_id: str) -> list[dict[str, Any]]:
    def _read(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT data FROM event WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [json.loads(row["data"]) for row in rows]

    return await db.run(_read)


class EventWriter:
    # 绑定 db 与 run_id；db 为 None 表示本次不持久化事件（未配置存储时的降级）
    def __init__(self, db: Database | None, run_id: str) -> None:
        self._db = db
        self._run_id = run_id
        self._open = False

    # 标记为已打开，供 async with 使用
    async def __aenter__(self) -> EventWriter:
        self._open = True
        return self

    # 标记为已关闭
    async def __aexit__(self, *args: object) -> None:
        self._open = False

    # 把事件写进 event 表；未打开或未配置 db 时静默跳过，写入失败只记日志不抛异常
    async def handle(self, event: BaseModel) -> None:
        if not self._open or self._db is None:
            return
        try:
            await self._db.transaction(lambda conn: _append_event(conn, self._run_id, event))
        except sqlite3.Error as e:
            logger.error("EventWriter: failed to write event: %s", e)

    # 将 handle 注册为 bus 的订阅者
    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(self.handle)
