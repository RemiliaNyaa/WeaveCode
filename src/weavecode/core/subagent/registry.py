from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult


# 后台子 Agent 登记表：派出去时先记账，agent_result 取结果时再结账。
# 它跟着 SpawnAgentTool 一起被注入，主 Agent 与子 Agent 共用同一个实例。
class BackgroundTaskRegistry:
    def __init__(self) -> None:
        # run_id -> (后台任务, 冷启动上下文)，上下文保留下来供结果召回时定位这次运行
        self._entries: dict[str, tuple[asyncio.Task[Any], Any]] = {}

    # 登记一个后台子 Agent
    def register(self, run_id: str, task: asyncio.Task[Any], context: Any) -> None:
        self._entries[run_id] = (task, context)

    # 取登记项；未登记返回 None
    def get(self, run_id: str) -> tuple[asyncio.Task[Any], Any] | None:
        return self._entries.get(run_id)

    # 还在跑的 run_id 列表
    def pending_ids(self) -> list[str]:
        return [run_id for run_id, (task, _) in self._entries.items() if not task.done()]

    # 结账：跑完的条目移出登记表，避免重复取结果时又等一遍
    def discard(self, run_id: str) -> None:
        self._entries.pop(run_id, None)


class AgentResultParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_id: str  # spawn_agent 返回的 run_id


# 取回后台子 Agent 的结果：没跑完只给提示不抛异常，由模型决定稍后再来
class AgentResultTool(BaseTool):
    params_model = AgentResultParams
    name = "agent_result"
    description = (
        "Retrieve the result of a subagent started with spawn_agent(background=true).\n"
        "Only runs registered by this agent can be queried; unknown run_id returns an error.\n"
        "If the subagent is still running you get a prompt to try again later."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "run_id": {
                "type": "string",
                "description": "The run_id returned by spawn_agent.",
            }
        },
        "required": ["run_id"],
    }

    def __init__(self, registry: BackgroundTaskRegistry) -> None:
        self._registry = registry

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = AgentResultParams.model_validate(params)
        entry = self._registry.get(p.run_id)
        if entry is None:
            return ToolResult(
                content=f"No background subagent registered for run_id={p.run_id}.",
                is_error=True,
                error_type="runtime_error",
            )
        task, _context = entry
        if not task.done():
            return ToolResult(
                content=(
                    f"Background subagent {p.run_id} is still running; "
                    "try agent_result again later."
                ),
                is_error=True,
                error_type="runtime_error",
            )
        self._registry.discard(p.run_id)
        try:
            result = task.result()
        except Exception as exc:  # 后台任务异常也建模成工具结果，交回给模型
            return ToolResult(
                content=f"Background subagent {p.run_id} failed: {exc}",
                is_error=True,
                error_type="runtime_error",
            )
        if isinstance(result, ToolResult):
            return result
        return ToolResult(content=str(result))
