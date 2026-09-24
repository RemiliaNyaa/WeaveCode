"""把旧的 ~/.weave/sessions/ 目录灌进 SQLite 存储层（一次性迁移工具）。

默认试运行：只扫描统计，不写库；确认无误后加 --apply 才真的写入。
注意脚本不幂等——message 与 event 会重复插入，跑过一次就不要再跑。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from weavecode.core.storage.migrations import apply

# 旧会话根目录与新库的默认位置
DEFAULT_SESSIONS_ROOT = Path("~/.weave/sessions")
DEFAULT_DB_PATH = Path("~/.weave/weave.db")


# 当前 epoch 毫秒（时间列统一存整数，便于排序比较）
def _now_ms() -> int:
    return int(time.time() * 1000)


# ISO 8601 字符串 → epoch 毫秒；解析不了就退回当前时间
def _iso_to_ms(iso: Any) -> int:
    if not isinstance(iso, str):
        return _now_ms()
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1000)
    except ValueError:
        return _now_ms()


# 逐行读取 JSONL，坏行跳过并告警
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            print(f"skip bad line {line_no} in {path}", file=sys.stderr)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


# 按工作目录算项目 id：规范化后的绝对路径直接当 id
def _project_id(worktree: str) -> str:
    return str(Path(worktree).resolve())


# 把一条消息的 content 拆成「块」列表：str 视为单个字符串块，list 原样
def _blocks_of(content: Any) -> list[Any]:
    return list(content) if isinstance(content, list) else [content]


# 写 project 与 session 两行（project 按 worktree upsert）
def _insert_session(conn: sqlite3.Connection, meta: dict[str, Any], worktree: str) -> str:
    session_id = str(meta["id"])
    pid = _project_id(worktree)
    now = _now_ms()
    conn.execute(
        "INSERT INTO project (id, worktree, name, time_created, time_updated)"
        " VALUES (?, ?, '', ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET time_updated = excluded.time_updated",
        (pid, worktree, now, now),
    )
    conn.execute(
        "INSERT INTO session (id, project_id, mode, status, title, directory,"
        " run_ids, time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(id) DO UPDATE SET status = excluded.status,"
        " title = excluded.title, directory = excluded.directory,"
        " run_ids = excluded.run_ids, time_updated = excluded.time_updated",
        (
            session_id,
            pid,
            str(meta.get("mode", "chat")),
            str(meta.get("status", "active")),
            str(meta.get("title", "")),
            worktree,
            json.dumps(meta.get("run_ids", []), ensure_ascii=False),
            _iso_to_ms(meta.get("created_at")),
            _iso_to_ms(meta.get("updated_at")),
        ),
    )
    return session_id


# 写一条消息：1 行 message + N 行 part
def _insert_message(
    conn: sqlite3.Connection, session_id: str, record: dict[str, Any], run_id: str | None
) -> None:
    role = str(record.get("role", ""))
    if role not in ("user", "assistant"):
        return
    cur = conn.execute(
        "INSERT INTO message (session_id, run_id, role, time_created) VALUES (?, ?, ?, ?)",
        (session_id, run_id, role, _iso_to_ms(record.get("ts"))),
    )
    message_id = int(cur.lastrowid or 0)
    for seq, block in enumerate(_blocks_of(record.get("content", ""))):
        conn.execute(
            "INSERT INTO part (message_id, seq, data) VALUES (?, ?, ?)",
            (message_id, seq, json.dumps(block, ensure_ascii=False)),
        )


# 写一次 run 的事件流：先给 run 建序号行，再按行序插入事件
def _insert_events(conn: sqlite3.Connection, run_id: str, records: list[dict[str, Any]]) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO event_sequence (run_id, seq) VALUES (?, ?)",
        (run_id, len(records)),
    )
    for seq, record in enumerate(records, start=1):
        ts = record.get("ts")
        event_type = str(record.get("type", ""))
        data = json.dumps(record, ensure_ascii=False)
        conn.execute(
            "INSERT INTO event (run_id, seq, type, data, time_created) VALUES (?, ?, ?, ?, ?)",
            (run_id, seq, event_type, data, _iso_to_ms(ts)),
        )
        conn.execute("UPDATE event_sequence SET seq = ? WHERE run_id = ?", (seq, run_id))


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="import_legacy_sessions", description="旧 sessions 目录导入 SQLite"
    )
    parser.add_argument("--root", default=str(DEFAULT_SESSIONS_ROOT), help="旧会话根目录")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="目标数据库文件")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只试运行）")
    args = parser.parse_args()

    root = Path(args.root).expanduser()
    if not root.is_dir():
        print(f"no sessions directory at {root}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(Path(args.db).expanduser())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    apply(conn)  # 幂等：建表 / 跑过就跳过

    sessions = 0
    messages = 0
    events = 0

    if args.apply:
        conn.execute("BEGIN IMMEDIATE")
    try:
        for session_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            meta_path = session_dir / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                print(f"skip unparsable meta.json in {session_dir}", file=sys.stderr)
                continue
            if not isinstance(meta, dict) or "id" not in meta:
                print(f"skip session without id: {session_dir}", file=sys.stderr)
                continue

            worktree = str(meta.get("working_dir") or Path.cwd())
            if args.apply:
                session_id = _insert_session(conn, meta, worktree)
            else:
                session_id = str(meta["id"])
            sessions += 1

            thread = _read_jsonl(session_dir / "thread.jsonl")
            for record in thread:
                if args.apply:
                    _insert_message(conn, session_id, record, None)
                messages += 1

            runs_dir = session_dir / "runs"
            run_dirs = sorted(p for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
            for run_dir in run_dirs:
                records = _read_jsonl(run_dir / "events.jsonl")
                if not records:
                    continue
                if args.apply:
                    _insert_events(conn, run_dir.name, records)
                events += len(records)

        if args.apply:
            conn.execute("COMMIT")
    except BaseException:
        if args.apply:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    mode = "applied" if args.apply else "dry-run"
    print(f"[{mode}] sessions={sessions} messages={messages} events={events}")
    if not args.apply:
        print("re-run with --apply to write these rows")
    else:
        print("warning: this script is not idempotent, do not run it again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
