from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from weavecode.core.subagent.runs import BackgroundRuns
from weavecode.core.tools.base import BaseTool, ToolResult


class WaitAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    run_ids: list[str] | None = None


# 阻塞等待后台子 Agent 出结果；省略 run_ids 表示等全部未完成的
class WaitAgentTool(BaseTool):
    name = "wait_agent"
    description = (
        "Wait for background sub-agents (started with spawn_agent background=true) to "
        "finish, and return their results.\n"
        "\n"
        "Rules:\n"
        "- Omit run_ids to wait for ALL background sub-agents that are still running.\n"
        "- Pass run_ids to wait for exactly those runs (they are waited concurrently).\n"
        "- This call blocks until the requested runs finish; their results come back "
        "inline.\n"
        "- You MUST collect every background sub-agent you started before ending your "
        "turn.\n"
        "- Results are kept, so waiting on the same run again is safe."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "run_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "run_ids to wait for. Omit to wait for all still-running background "
                    "sub-agents."
                ),
            },
        },
        "required": [],
    }
    params_model = WaitAgentParams

    # 注入后台子 Agent 登记表（与 SpawnAgentTool 共用同一份，由 runner 创建）
    def __init__(self, runs: BackgroundRuns) -> None:
        self._runs = runs

    # 阻塞等待指定（或全部未完成）的后台子 Agent，把各自结果拼成文本返回
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WaitAgentParams.model_validate(params)
        targets = p.run_ids or None

        if targets is None and not self._runs.known_ids():
            return ToolResult(content="No background sub-agents were started.")

        outcomes = await self._runs.wait(targets)
        if not outcomes:
            known = ", ".join(self._runs.known_ids()) or "none"
            return ToolResult(
                content=f"No background sub-agent matched run_ids={targets}. Known runs: {known}.",
                is_error=True,
                error_type="runtime_error",
            )

        blocks = [f"[{o.run_id}] status={o.status}\n{o.result}" for o in outcomes]
        return ToolResult(content="\n\n".join(blocks))
