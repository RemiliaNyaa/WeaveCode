from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from weavecode.core.tools.base import BaseTool
from weavecode.core.tools.builtin import (
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from weavecode.core.tools.builtin.edit_file import EditFileTool


# 造一个工具实例 + 它需要的最小调用参数（目标文件由用例预先创建）
def _case(tool_name: str, root: Path) -> tuple[BaseTool, dict[str, object]]:
    if tool_name == "read_file":
        return ReadFileTool(), {"path": str(root / "a.txt")}
    if tool_name == "write_file":
        return WriteFileTool(), {"path": str(root / "w.txt"), "content": "x"}
    if tool_name == "edit_file":
        return EditFileTool(), {
            "path": str(root / "e.txt"),
            "old_string": "a",
            "new_string": "b",
        }
    if tool_name == "list_dir":
        return ListDirTool(), {"path": str(root)}
    if tool_name == "glob":
        return GlobTool(), {"path": str(root), "pattern": "*.txt"}
    return GrepTool(), {"path": str(root), "pattern": "a"}


# 功能：六个工具（含 3 个目录/检索工具）的 invoke 都必须让出事件循环——磁盘 IO 确实进了线程
# 设计：用「哨兵任务是否已被调度」来判定，而不是测耗时——
#       invoke 若全程不让出（= IO 还在主线程同步跑），哨兵任务根本没机会执行，断言必失败
@pytest.mark.parametrize(
    "tool_name",
    ["read_file", "write_file", "edit_file", "list_dir", "glob", "grep"],
)
@pytest.mark.asyncio
async def test_tool_io_yields_to_the_event_loop(tool_name: str, tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "e.txt").write_text("a\n", encoding="utf-8")
    tool, params = _case(tool_name, tmp_path)

    async def sentinel() -> None:
        return

    task = asyncio.create_task(sentinel())
    result = await tool.invoke(params)

    assert not result.is_error, result.content
    assert task.done(), f"{tool_name}.invoke 全程没有让出事件循环（磁盘 IO 没进线程）"
