from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from weavecode.core.task.manager import TaskManager
from weavecode.core.tools.base import BaseTool, ToolResult


class TaskCreateParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    subject: str
    description: str = ""
    blocked_by: list[int] = Field(default_factory=list)


class TaskCreateTool(BaseTool):
    """创建任务：写入 task_{id}.json 并自增编号。"""

    params_model = TaskCreateParams
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
        p = TaskCreateParams.model_validate(params)
        subject = p.subject.strip()
        if not subject:
            return ToolResult(
                content="subject is required",
                is_error=True,
                error_type="runtime_error",
            )

        task = self._manager.create(subject, p.description, list(p.blocked_by))
        msg = f"Created task #{task.id}: {task.subject}"
        if task.blocked_by:
            msg += f" (blocked by: {task.blocked_by})"
        return ToolResult(content=msg)
