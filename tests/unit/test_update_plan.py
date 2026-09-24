from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from weavecode.core.storage import Database, apply_migrations
from weavecode.core.tools.builtin.update_plan import (
    DbPlanStorage,
    NoopPlanStorage,
    UpdatePlanTool,
)


# 造一个建好表的 db，并预置 project + session 各一行（todo 有外键，必须先有 session）
async def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    await apply_migrations(db)

    def _seed(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO project (id, worktree, name, time_created, time_updated) "
            "VALUES ('p1', '/proj', '', 0, 0)"
        )
        conn.execute(
            "INSERT INTO session (id, project_id, mode, status, title, directory, run_ids, "
            "time_created, time_updated) VALUES ('s1', 'p1', 'chat', 'active', '', '/proj', "
            "'[]', 0, 0)"
        )

    await db.run(_seed)
    return db


# 读出某 session 的任务行（按 seq 排序），用于断言落库结果
async def _rows(db: Database, sid: str = "s1") -> list[tuple[str, str]]:
    def _read(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        rows = conn.execute(
            "SELECT step, status FROM todo WHERE session_id = ? ORDER BY seq", (sid,)
        ).fetchall()
        return [(str(r["step"]), str(r["status"])) for r in rows]

    return await db.run(_read)


# 功能：update_plan 把任务列表写进 todo 表（step 与 status 两列都对）
# 设计：注入 DbPlanStorage，从 todo 表读回断言 —— 任务列表已从 tasks.json 改存 SQLite，必须验证真的落库
async def test_update_plan_writes_rows(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(DbPlanStorage(db, "s1"))

    result = await tool.invoke(
        {
            "tasks": [
                {"step": "设计接口", "status": "pending"},
                {"step": "实现登录", "status": "in_progress"},
            ]
        }
    )

    assert not result.is_error
    assert result.content == "任务列表更新成功"
    assert await _rows(db) == [("设计接口", "pending"), ("实现登录", "in_progress")]


# 功能：多次调用是全量覆盖，旧任务被替换
# 设计：先写两项再写一项，断言表里只剩最新一项 —— 与旧文件版「整体覆写 tasks.json」的语义保持一致
async def test_update_plan_overwrites(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(DbPlanStorage(db, "s1"))
    await tool.invoke({"tasks": [{"step": "a", "status": "pending"}]})
    await tool.invoke({"tasks": [{"step": "b", "status": "completed"}]})
    assert await _rows(db) == [("b", "completed")]


# 功能：读回的先后顺序 = 传入列表的顺序（靠 seq 列，不靠自增 id）
# 设计：写 3 项后按 seq 读回，断言顺序一致 —— 这是 todo 表专门留一列 seq 的理由（DB 不像 JSON 数组自带顺序）
async def test_update_plan_preserves_order(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(DbPlanStorage(db, "s1"))
    await tool.invoke(
        {
            "tasks": [
                {"step": "第一步", "status": "completed"},
                {"step": "第二步", "status": "in_progress"},
                {"step": "第三步", "status": "pending"},
            ]
        }
    )
    assert await _rows(db) == [
        ("第一步", "completed"),
        ("第二步", "in_progress"),
        ("第三步", "pending"),
    ]


# 功能：NoopPlanStorage 不写任何东西，返回仍成功
# 设计：注入 NoopPlanStorage，断言 todo 表里没有该 session 的行且 is_error 为 False —— 子 Agent 的降级路径
async def test_noop_storage_writes_nothing(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(NoopPlanStorage())
    result = await tool.invoke({"tasks": [{"step": "x", "status": "pending"}]})
    assert not result.is_error
    assert result.content == "任务列表更新成功"
    assert await _rows(db) == []


# 功能：状态不做校验，任意字符串都能存
# 设计：传非法 status（cancelled/done），断言工具不报错、原样落库
async def test_update_plan_no_status_validation(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(DbPlanStorage(db, "s1"))
    result = await tool.invoke(
        {"tasks": [{"step": "x", "status": "cancelled"}, {"step": "y", "status": "done"}]}
    )
    assert not result.is_error
    assert await _rows(db) == [("x", "cancelled"), ("y", "done")]


# 功能：删掉 session 时其任务行被外键级联清空
# 设计：写入任务后删 session，断言 todo 表跟着清空 —— 证明任务列表确实归属 session（外键生效）
async def test_todo_rows_cascade_on_session_delete(tmp_path: Path) -> None:
    db = await _db(tmp_path)
    tool = UpdatePlanTool(DbPlanStorage(db, "s1"))
    await tool.invoke({"tasks": [{"step": "x", "status": "pending"}]})

    def _drop(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM session WHERE id = 's1'")

    await db.run(_drop)
    assert await _rows(db) == []


# 功能：参数缺少 tasks 时抛 ValidationError（schema_error 触发）
# 设计：传空字典，预期 pydantic 拒绝缺必填字段
async def test_update_plan_missing_tasks() -> None:
    tool = UpdatePlanTool(NoopPlanStorage())
    with pytest.raises(Exception):
        await tool.invoke({})


# 功能：UpdatePlanParams 忽略额外字段（extra="ignore"）
# 设计：多传无关字段，断言不报错且只取 tasks
async def test_update_plan_extra_ignored() -> None:
    tool = UpdatePlanTool(NoopPlanStorage())
    result = await tool.invoke(
        {"tasks": [{"step": "a", "status": "pending"}], "explanation": "why"}
    )
    assert not result.is_error
    assert result.content == "任务列表更新成功"
