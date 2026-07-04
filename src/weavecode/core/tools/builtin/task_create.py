from __future__ import annotations

from weavecode.core.task.manager import TaskManager
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskCreateTool(BaseTool):
    """创建任务：写入 task_{id}.json 并自增编号。"""

    name = "task_create"
    description = (
        "Create a task in the shared task list.\n"
        "subject is the task title; description is optional detail.\n"
        "blocked_by lists ids of tasks that must finish before this one can start."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "Short title of the task.",
            },
            "description": {
                "type": "string",
                "description": "Optional detail about the task.",
            },
            "blocked_by": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Ids of tasks that must finish first.",
            },
        },
        "required": ["subject"],
    }

    # 四个任务工具共享同一个 TaskManager 实例，写入的文件彼此可见
    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        subject = str(params.get("subject", "")).strip()
        if not subject:
            return ToolResult(content="subject is required", is_error=True)

        description = str(params.get("description", ""))
        raw = params.get("blocked_by") or []
        if not isinstance(raw, list):
            return ToolResult(
                content="blocked_by must be a list of task ids", is_error=True
            )
        try:
            blocked_by = [int(x) for x in raw]
        except (TypeError, ValueError):
            return ToolResult(
                content="blocked_by must contain integer task ids", is_error=True
            )

        task = self._manager.create(subject, description, blocked_by)
        msg = f"Created task #{task.id}: {task.subject}"
        if task.blocked_by:
            msg += f" (blocked by: {task.blocked_by})"
        return ToolResult(content=msg)
