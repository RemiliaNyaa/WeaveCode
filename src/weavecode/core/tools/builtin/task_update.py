from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from weavecode.core.task.manager import TaskManager
from weavecode.core.task.model import TASK_STATUSES
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskUpdateParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    task_id: int
    status: str | None = None
    description: str | None = None


class TaskUpdateTool(BaseTool):
    """按 id 更新任务状态与描述，写回 task_{id}.json。"""

    params_model = TaskUpdateParams
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
        p = TaskUpdateParams.model_validate(params)
        if p.status is None and p.description is None:
            return ToolResult(
                content="nothing to update: pass status and/or description",
                is_error=True,
                error_type="runtime_error",
            )

        if p.status is not None and p.status not in TASK_STATUSES:
            return ToolResult(
                content=(
                    f"invalid status: {p.status} "
                    f"(expected one of {', '.join(TASK_STATUSES)})"
                ),
                is_error=True,
                error_type="runtime_error",
            )

        try:
            task = self._manager.update(
                p.task_id,
                status=p.status,
                description=p.description,
            )
        except KeyError as exc:
            return ToolResult(
                content=str(exc), is_error=True, error_type="runtime_error"
            )
        return ToolResult(content=f"Updated task #{task.id}: {task.status}")
