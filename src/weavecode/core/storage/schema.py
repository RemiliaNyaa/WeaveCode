from __future__ import annotations

import sqlite3

from weavecode.core.storage.migrations import MIGRATIONS

# 迁移 1：会话与消息（对齐 opencode 的 project / session / message / part）
#
# 设计要点（照抄 opencode 的实证结论）：
#   - message 与 part **拆两张表**，但正文都存 JSON 列（part.data 是单个 content block）
#   - 「按项目隔离」靠 session.project_id 外键，不是分库/分目录
#   - 时间统一存 epoch 毫秒（整数，便于排序与比较）
_STATEMENTS_1 = (
    "CREATE TABLE project ("
    "  id TEXT PRIMARY KEY,"
    "  worktree TEXT NOT NULL UNIQUE,"  # 项目根目录（绝对路径）
    "  name TEXT NOT NULL DEFAULT '',"
    "  time_created INTEGER NOT NULL,"
    "  time_updated INTEGER NOT NULL"
    ")",
    "CREATE TABLE session ("
    "  id TEXT PRIMARY KEY,"
    "  project_id TEXT NOT NULL REFERENCES project(id) ON DELETE CASCADE,"
    "  mode TEXT NOT NULL,"  # one_shot | chat
    "  status TEXT NOT NULL,"  # active | waiting_for_input | closed
    "  title TEXT NOT NULL DEFAULT '',"
    "  directory TEXT NOT NULL,"  # 会话工作目录（绝对路径）
    "  run_ids TEXT NOT NULL DEFAULT '[]',"  # JSON 数组（本会话跑过的 run id）
    "  time_created INTEGER NOT NULL,"
    "  time_updated INTEGER NOT NULL"
    ")",
    "CREATE INDEX session_project_idx ON session(project_id)",
    "CREATE TABLE message ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  session_id TEXT NOT NULL REFERENCES session(id) ON DELETE CASCADE,"
    "  run_id TEXT,"
    "  role TEXT NOT NULL,"  # user | assistant
    "  time_created INTEGER NOT NULL"
    ")",
    "CREATE INDEX message_session_idx ON message(session_id, id)",
    "CREATE TABLE part ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  message_id INTEGER NOT NULL REFERENCES message(id) ON DELETE CASCADE,"
    "  seq INTEGER NOT NULL,"  # 同一条消息内的块顺序
    "  data TEXT NOT NULL"  # 单个 content block 的 JSON
    ")",
    "CREATE INDEX part_message_idx ON part(message_id, seq)",
)


# 建 project / session / message / part 四张表
def _migration_1(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS_1:
        conn.execute(statement)


# 迁移 2：事件流（对齐 opencode 的 event / event_sequence）
#
# 为什么 event_sequence 单独一张表：JSONL 靠「行序」天然有序，DB 里必须**显式存序号**；
# 序号又要在事务里原子地「读最大值 → +1 → 写回」，所以单独一行（run_id 主键）承载它。
_STATEMENTS_2 = (
    "CREATE TABLE event_sequence ("
    "  run_id TEXT PRIMARY KEY,"
    "  seq INTEGER NOT NULL"  # 该 run 已分配到的最大序号
    ")",
    "CREATE TABLE event ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  run_id TEXT NOT NULL REFERENCES event_sequence(run_id) ON DELETE CASCADE,"
    "  seq INTEGER NOT NULL,"
    "  type TEXT NOT NULL,"  # 事件类型，如 tool.call_started
    "  data TEXT NOT NULL,"  # 事件完整 JSON（回放时原样还原）
    "  time_created INTEGER NOT NULL"
    ")",
    "CREATE UNIQUE INDEX event_run_seq_idx ON event(run_id, seq)",
    "CREATE INDEX event_run_type_idx ON event(run_id, type, seq)",
)


# 建 event / event_sequence 两张表
def _migration_2(conn: sqlite3.Connection) -> None:
    for statement in _STATEMENTS_2:
        conn.execute(statement)


# 注册迁移（版本号从 1 起，只往后追加）
MIGRATIONS.append((1, "session_and_messages", _migration_1))
MIGRATIONS.append((2, "event_stream", _migration_2))
