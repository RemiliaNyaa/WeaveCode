from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult

_SUCCEEDED_MSG = "任务列表更新成功"


class PlanItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    step: str
    status: str


class UpdatePlanParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tasks: list[PlanItem]


# 计划存储后端抽象：主 Agent 写文件、子 Agent 空操作，由构造方注入
class PlanStorage(Protocol):
    # 保存任务列表；实现方决定是否落盘
    def save(self, tasks: list[PlanItem]) -> None: ...


# 主 Agent 后端：把任务列表全量覆盖写入 {dir}/tasks.json
class FilePlanStorage:
    def __init__(self, directory: Path) -> None:
        self._file = directory / "tasks.json"

    # 全量覆盖写入 JSON 文件（自动创建父目录），无校验直接落盘
    def save(self, tasks: list[PlanItem]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        data = [{"step": t.step, "status": t.status} for t in tasks]
        self._file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


# 子 Agent 后端：不落盘，什么都不做（返回永远成功）
class NoopPlanStorage:
    # 空操作：子 Agent 的任务列表不写入任何文件
    def save(self, tasks: list[PlanItem]) -> None:
        return


class UpdatePlanTool(BaseTool):
    params_model = UpdatePlanParams
    name = "update_plan"
    description = (
        "Update the task plan (a checklist of steps and their status).\n"
        "\n"
        "When to use:\n"
        "- The task is complex and multi-step: multiple distinct actions, logical "
        "stages, dependencies between steps, or ambiguity that benefits from a "
        "visible roadmap.\n"
        "- The user explicitly asks for a plan or todo list.\n"
        "\n"
        "When NOT to use:\n"
        "- Simple or single-step requests that you can just do immediately.\n"
        "- Purely informational or conversational requests.\n"
        "- Tracking would add no organizational value.\n"
        "\n"
        "Rules:\n"
        "- Pass the FULL list of plan items; the whole list is replaced by what you pass.\n"
        "- Each item has step (task description) and status: "
        "pending | in_progress | completed.\n"
        "- At most one item may be in_progress at a time.\n"
        "- As you finish a step, update the plan: mark it completed, mark the next one "
        "in_progress. When all steps are done, mark the final one completed before "
        "giving your final answer."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "step": {"type": "string", "description": "Task step text."},
                        "status": {
                            "type": "string",
                            "description": "Step status: pending, in_progress, or completed.",
                        },
                    },
                    "required": ["step", "status"],
                },
                "description": "The full list of plan items.",
            },
        },
        "required": ["tasks"],
    }

    # 持有 PlanStorage 实例（主 Agent 传 File、子 Agent 传 Noop），供 invoke 调用
    def __init__(self, storage: PlanStorage) -> None:
        self._storage = storage

    # 全量保存任务列表到注入的后端；不校验状态，永远返回"任务列表更新成功"
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = UpdatePlanParams.model_validate(params)
        self._storage.save(p.tasks)
        return ToolResult(content=_SUCCEEDED_MSG)
