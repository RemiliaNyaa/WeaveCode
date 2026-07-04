from __future__ import annotations

from weavecode.core.task.manager import TaskManager
from weavecode.core.task.model import TASK_STATUSES, Task
from weavecode.core.tools.base import BaseTool, ToolResult

# 状态 → 列表标记，让模型一眼看出哪些能开工、哪些还在等
_MARKS = {
    "pending": "[ ]",
    "in_progress": "[>]",
    "completed": "[x]",
    "cancelled": "[-]",
}


class TaskListTool(BaseTool):
    """列出全部任务及状态，按 id 排序，可按状态过滤。"""

    name = "task_list"
    description = (
        "List every task with its status, sorted by id.\n"
        f"Optionally filter with status: {', '.join(TASK_STATUSES)}."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": f"Only list tasks with this status ({', '.join(TASK_STATUSES)}).",
            },
        },
        "required": [],
    }

    # 四个任务工具共享同一个 TaskManager 实例，看到的是同一批任务文件
    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        raw = params.get("status")
        status = None if raw in (None, "") else str(raw)
        if status is not None and status not in TASK_STATUSES:
            return ToolResult(
                content=(
                    f"invalid status: {status} "
                    f"(expected one of {', '.join(TASK_STATUSES)})"
                ),
                is_error=True,
            )

        tasks = self._manager.list(status)
        if not tasks:
            return ToolResult(content="No tasks.")
        return ToolResult(content="\n".join(_format(task) for task in tasks))


# 单行紧凑表示：[ ] #1: 主题（有前置依赖时附带等待清单）
def _format(task: Task) -> str:
    line = f"{_MARKS.get(task.status, '[ ]')} #{task.id}: {task.subject}"
    if task.blocked_by:
        line += f" (blocked by: {task.blocked_by})"
    return line
