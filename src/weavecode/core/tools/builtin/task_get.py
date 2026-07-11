from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from weavecode.core.task.manager import TaskManager
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskGetParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: int


class TaskGetTool(BaseTool):
    """按 id 取单个任务的六字段详情。"""

    params_model = TaskGetParams
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
        p = TaskGetParams.model_validate(params)
        task = self._manager.get(p.task_id)
        if task is None:
            return ToolResult(
                content=f"Task #{p.task_id} not found.",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(
            content=json.dumps(task.to_dict(), indent=2, ensure_ascii=False)
        )
