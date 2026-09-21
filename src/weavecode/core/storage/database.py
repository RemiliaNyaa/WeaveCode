from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

log = logging.getLogger(__name__)

# 默认数据库位置：全局单文件（对齐 opencode 的 <data>/opencode.db）
DEFAULT_DB_PATH = Path("~/.weave/weave.db")

# 连接级 PRAGMA（对齐 opencode 的 database.ts）：
#   WAL            读写不互相阻塞（跨进程靠它 + busy_timeout 协调）
#   synchronous    WAL 下的推荐级别（性能与安全折中）
#   busy_timeout   跨进程争用时等 5 秒再报错，而不是立刻失败
#   foreign_keys   打开外键约束（session.project_id → project.id 这类要靠它）
_PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA busy_timeout = 5000",
    "PRAGMA foreign_keys = ON",
)


# SQLite 存储：一个进程一个连接 + 一把锁串行化所有语句/事务
#
# 为什么两样都要：
#   ① sqlite3 连接不是线程安全的，而我们把同步逻辑放进线程池跑（改动三十的套路）；
#   ② 并发语句进来时必须串行 —— 对齐 opencode 的 Semaphore(1) 做法。
class Database:
    def __init__(self, path: Path | None = None) -> None:
        self._path = (path or DEFAULT_DB_PATH).expanduser()
        self._lock = asyncio.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    # 在锁内、线程里执行一段同步逻辑（action 拿到连接），返回其结果
    async def run(self, action: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            return await asyncio.to_thread(self._execute, action)

    # 在锁内、线程里执行一个 immediate 事务；抛异常自动回滚
    async def transaction(self, action: Callable[[sqlite3.Connection], T]) -> T:
        return await self.run(lambda conn: _in_transaction(conn, action))

    # 同步核心：确保连接已开 → 执行 action（锁已由 run 持有）
    def _execute(self, action: Callable[[sqlite3.Connection], T]) -> T:
        return action(self._ensure_conn())

    # 打开连接并设好 PRAGMA；已打开则复用（幂等）
    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → 关掉 sqlite3 的隐式事务，由我们显式 BEGIN/COMMIT
        conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        log.info("db: opened %s", self._path)
        self._conn = conn
        return conn


# 在一个 immediate 事务里跑 action；抛异常回滚后原样抛出
def _in_transaction(conn: sqlite3.Connection, action: Callable[[sqlite3.Connection], T]) -> T:
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = action(conn)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return result
