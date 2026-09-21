from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from weavecode.core.session.model import Session
from weavecode.core.storage.database import Database

logger = logging.getLogger(__name__)

MessageContent = str | list[dict[str, Any]]


# 返回当前 epoch 毫秒（时间列统一存整数，便于排序比较）
def _now_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


# ISO 8601 字符串 → epoch 毫秒（写库用；解析不了就退回当前时间）
def _iso_to_ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return _now_ms()


# epoch 毫秒 → ISO 8601 字符串（读库用）
def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


# 按工作目录算项目 id：规范化后的绝对路径直接当 id（可读；worktree 另有 UNIQUE 约束兜底）
def _project_id(worktree: str) -> str:
    return str(Path(worktree).resolve())


# 把一条消息的 content 拆成「块」列表：str 视为单个字符串块，list 原样
def _blocks_of(content: Any) -> list[Any]:
    return list(content) if isinstance(content, list) else [content]


# 决定一条消息的 content 形态：只有一个字符串块 → 用 str；否则用 list（保证往返一致）
def _content_of(blocks: list[Any]) -> Any:
    if len(blocks) == 1 and isinstance(blocks[0], str):
        return blocks[0]
    return blocks


# 往 message / part 两张表插一批消息（调用方负责开事务）
def _insert_messages(
    conn: sqlite3.Connection, sid: str, messages: list[dict[str, Any]], run_id: str
) -> None:
    now = _now_ms()
    for msg in messages:
        cur = conn.execute(
            "INSERT INTO message (session_id, run_id, role, time_created) VALUES (?, ?, ?, ?)",
            (sid, run_id or None, str(msg["role"]), now),
        )
        mid = int(cur.lastrowid or 0)
        for seq, block in enumerate(_blocks_of(msg.get("content", ""))):
            conn.execute(
                "INSERT INTO part (message_id, seq, data) VALUES (?, ?, ?)",
                (mid, seq, json.dumps(block, ensure_ascii=False)),
            )


# 把「message + part」查询结果重新拼成 messages：同一 message_id 的 part 合成一条
def _assemble(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    current_id: int | None = None
    role = ""
    blocks: list[Any] = []
    for row in rows:
        if row["message_id"] != current_id:
            if current_id is not None:
                messages.append({"role": role, "content": _content_of(blocks)})
            current_id = int(row["message_id"])
            role = str(row["role"])
            blocks = []
        blocks.append(json.loads(row["data"]))
    if current_id is not None:
        messages.append({"role": role, "content": _content_of(blocks)})
    return messages


class SessionStore:
    # 初始化：只接 db —— 会话数据全在 SQLite 里，不再有文件根目录
    def __init__(self, db: Database) -> None:
        self._db = db

    # 暴露底层 db（SessionManager 建 DbPlanStorage 时要用同一个连接）
    @property
    def db(self) -> Database:
        return self._db

    # 将 session meta 写入 session 表（project 按 worktree upsert）
    async def write_meta(self, session: Session) -> None:
        worktree = session.effective_working_dir()
        pid = _project_id(worktree)

        def _write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO project (id, worktree, name, time_created, time_updated)"
                " VALUES (?, ?, '', ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET time_updated = excluded.time_updated",
                (pid, worktree, _now_ms(), _now_ms()),
            )
            conn.execute(
                "INSERT INTO session (id, project_id, mode, status, title, directory,"
                " run_ids, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET status = excluded.status,"
                " title = excluded.title, directory = excluded.directory,"
                " run_ids = excluded.run_ids, time_updated = excluded.time_updated",
                (
                    session.id,
                    pid,
                    session.mode,
                    session.status,
                    session.title,
                    session.working_dir,
                    json.dumps(session.run_ids, ensure_ascii=False),
                    _iso_to_ms(session.created_at),
                    _iso_to_ms(session.updated_at),
                ),
            )

        await self._db.transaction(_write)

    # 从 session 表读取 session meta；不存在时抛 FileNotFoundError
    async def read_meta(self, sid: str) -> Session:
        def _read(conn: sqlite3.Connection) -> sqlite3.Row | None:
            row: sqlite3.Row | None = conn.execute(
                "SELECT * FROM session WHERE id = ?", (sid,)
            ).fetchone()
            return row

        row = await self._db.run(_read)
        if row is None:
            raise FileNotFoundError(f"session not found: {sid}")
        return Session(
            id=str(row["id"]),
            mode=str(row["mode"]),  # type: ignore[arg-type]
            status=str(row["status"]),  # type: ignore[arg-type]
            title=str(row["title"]),
            created_at=_ms_to_iso(int(row["time_created"])),
            updated_at=_ms_to_iso(int(row["time_updated"])),
            run_ids=[str(x) for x in json.loads(row["run_ids"])],
            working_dir=str(row["directory"]),
        )

    # 追加一条 Anthropic API 消息（1 行 message + N 行 part）
    async def append_message(
        self, sid: str, role: str, content: MessageContent, run_id: str | None = None
    ) -> None:
        await self.append_messages(sid, [{"role": role, "content": content}], run_id=run_id or "")

    # 批量追加一次 run 新产生的消息（一个事务里写 message + part）
    async def append_messages(
        self, sid: str, messages: list[dict[str, Any]], run_id: str
    ) -> None:
        await self._db.transaction(lambda conn: _insert_messages(conn, sid, messages, run_id))

    # 读取完整 thread 并拼回可直接传给 Anthropic 的 messages
    async def read_messages(self, sid: str) -> list[dict[str, Any]]:
        def _read(conn: sqlite3.Connection) -> list[sqlite3.Row]:
            return conn.execute(
                "SELECT m.id AS message_id, m.role AS role, p.data AS data"
                " FROM message m JOIN part p ON p.message_id = m.id"
                " WHERE m.session_id = ? AND m.role IN ('user', 'assistant')"
                " ORDER BY m.id, p.seq",
                (sid,),
            ).fetchall()

        messages = _assemble(await self._db.run(_read))
        messages = self._trim_orphan_tool_use(messages)
        from weavecode.core.compact.budget import truncate_tool_results
        return truncate_tool_results(messages)

    # 裁掉尾部未配对 tool_use 以及其后的消息，避免 Anthropic messages.invalid
    def _trim_orphan_tool_use(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        pending: set[str] = set()
        last_balanced = 0
        for idx, msg in enumerate(messages, start=1):
            content = msg.get("content")
            if isinstance(content, list):
                if msg.get("role") == "assistant":
                    for block in content:
                        if block.get("type") == "tool_use":
                            pending.add(str(block.get("id", "")))
                elif msg.get("role") == "user":
                    for block in content:
                        if block.get("type") == "tool_result":
                            pending.discard(str(block.get("tool_use_id", "")))
            if not pending:
                last_balanced = idx
        if pending:
            logger.warning("trim orphan tool_use blocks from thread")
            return messages[:last_balanced]
        return messages

    # 用压缩后的消息整体替换 thread（一个事务：先删旧 message，再插新的）
    async def write_compacted(self, sid: str, messages: list[dict[str, Any]]) -> None:
        def _replace(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM message WHERE session_id = ?", (sid,))
            _insert_messages(conn, sid, messages, run_id="")

        await self._db.transaction(_replace)
