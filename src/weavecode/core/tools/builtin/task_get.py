from __future__ import annotations

import json

from weavecode.core.task.manager import TaskManager
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskGetTool(BaseTool):
    """按 id 取单个任务的六字段详情。"""

    name = "task_get"
    description = "Get the full details of one task by its id, as JSON."
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "integer",
                "description": "Id of the task to read.",
            },
        },
        "required": ["task_id"],
    }

    # 四个任务工具共享同一个 TaskManager 实例，读到的是同一批任务文件
    def __init__(self, manager: TaskManager) -> None:
        self._manager = manager

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        task_id = _as_int(params.get("task_id"))
        if task_id is None:
            return ToolResult(content="task_id must be an integer", is_error=True)

        task = self._manager.get(task_id)
        if task is None:
            return ToolResult(content=f"Task #{task_id} not found.", is_error=True)
        return ToolResult(
            content=json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
        )


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
