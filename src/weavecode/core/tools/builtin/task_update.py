from __future__ import annotations

from weavecode.core.task.manager import TaskManager
from weavecode.core.task.model import TASK_STATUSES
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskUpdateTool(BaseTool):
    """按 id 更新任务状态与描述，写回 task_{id}.json。"""

    name = "task_update"
    description = (
        "Update the status and/or description of an existing task.\n"
        f"status must be one of: {', '.join(TASK_STATUSES)}.\n"
        "Marking a task completed also unblocks the tasks waiting on it."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "Id of the task to update.",
            },
            "status": {
                "type": "string",
                "description": f"New status: {', '.join(TASK_STATUSES)}.",
            },
            "description": {
                "type": "string",
                "description": "Replacement description for the task.",
            },
        },
        "required": ["task_id"],
    }

    # 四个任务工具共享同一个 TaskManager 实例，改动对其他任务工具立即可见
    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        task_id = _as_int(params.get("task_id"))
        if task_id is None:
            return ToolResult(content="task_id must be an integer", is_error=True)

        status = params.get("status")
        description = params.get("description")
        if status is None and description is None:
            return ToolResult(
                content="nothing to update: pass status and/or description",
                is_error=True,
            )

        status_str = None if status is None else str(status)
        if status_str is not None and status_str not in TASK_STATUSES:
            return ToolResult(
                content=(
                    f"invalid status: {status_str} "
                    f"(expected one of {', '.join(TASK_STATUSES)})"
                ),
                is_error=True,
            )

        try:
            task = self._manager.update(
                task_id,
                status=status_str,
                description=None if description is None else str(description),
            )
        except KeyError as exc:
            return ToolResult(content=str(exc), is_error=True)
        return ToolResult(content=f"Updated task #{task.id}: {task.status}")


# 把参数转成整数 id；转不动返回 None
def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None
