from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.loop import AgentLoop
from weavecode.core.runs import new_run_id
from weavecode.core.subagent.registry import AgentResultTool, BackgroundTaskRegistry
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.builtin import BashTool, ListDirTool, ReadFileTool, WriteFileTool
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.llm.base import LLMProvider
    from weavecode.core.permissions.manager import PermissionManager


class SpawnAgentParams(BaseModel):
    description: str
    prompt: str
    # 后台派生：立即拿到 run_id，稍后用 agent_result 取结果
    run_in_background: bool = False


# 在隔离的冷启动上下文中派生子 agent；默认阻塞等结果
class SpawnAgentTool(BaseTool):
    name = "spawn_agent"
    description = (
        "Spawn an isolated sub-agent to handle a self-contained sub-task.\n"
        "\n"
        "When to use:\n"
        "- A sub-task is large, independent, and would add many tool calls and tokens "
        "to the current conversation.\n"
        "- The sub-task needs a fresh context (e.g. exploring a separate concern).\n"
        "\n"
        "When NOT to use:\n"
        "- Small steps you can do directly - just do them.\n"
        "- The sub-task depends heavily on current conversation context - keep it "
        "in the main thread or pass ALL needed context explicitly in prompt.\n"
        "\n"
        "Rules:\n"
        "- By default this tool BLOCKS: it waits for the sub-agent to finish and the "
        "result is returned inline as the tool output.\n"
        "- Set run_in_background=true to start the sub-agent without waiting: the call "
        "returns immediately with a run_id, and you collect the result later with "
        "agent_result(run_id=...).\n"
        "- To run several sub-agents in parallel, issue MULTIPLE spawn_agent tool calls "
        "in a single tool-calling step (one call per sub-task). The system executes them "
        "concurrently. Do NOT wait for one to finish before issuing the next.\n"
        "- The sub-agent starts with a clean context containing ONLY the provided prompt "
        "- it does NOT inherit the current conversation history. Be explicit and "
        "self-contained in prompt.\n"
        "- The sub-agent may spawn further sub-agents up to a nesting limit of 2.\n"
        "- description should be a short 3-5 word label shown in progress display."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "3-5 word task description shown in progress display",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "Complete task description including all context the sub-agent needs. "
                    "The sub-agent cannot see the parent conversation, so be explicit."
                ),
            },
            "run_in_background": {
                "type": "boolean",
                "description": (
                    "Start the sub-agent without waiting and return a run_id immediately "
                    "(collect the result later with agent_result). Defaults to false."
                ),
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造派生工具：depth 记录当前所处的嵌套层级，主 Agent 为 0
    def __init__(
        self,
        provider: LLMProvider,
        permission_manager: PermissionManager | None,
        max_steps: int,
        session_id: str,
        task_registry: BackgroundTaskRegistry,
        depth: int = 0,
    ) -> None:
        self._provider = provider
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._session_id = session_id
        self._task_registry = task_registry
        self._depth = depth

    # 派生子 agent：默认阻塞等结果，run_in_background=true 时登记后台任务并立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)
        if self._depth >= 2:
            return ToolResult(
                content="Subagent nesting limit (2) reached; cannot spawn further subagents.",
                is_error=True,
            )
        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
        )
        if p.run_in_background:
            task = asyncio.create_task(self._run_child(child_context, p))
            self._task_registry.register(child_run_id, task, child_context)
            return ToolResult(
                content=(
                    f"Subagent started in background. run_id={child_run_id}. "
                    f"Use agent_result(run_id='{child_run_id}') to retrieve result."
                )
            )
        return await self._run_child(child_context, p)

    # 跑一个子 Agent：冷启动上下文 + 全新事件总线，返回它的结果
    async def _run_child(self, child_context: ExecutionContext, p: SpawnAgentParams) -> ToolResult:
        child_bus = EventBus()
        child_registry = self._build_child_registry()
        child_loop = AgentLoop(
            self._provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
        )
        await child_loop.run(child_context)

        if child_context.status == "success":
            return ToolResult(
                content=child_context.result or "Subagent completed with no text output."
            )
        return ToolResult(
            content=(
                child_context.result
                or f"Subagent failed (status={child_context.status}, reason={child_context.reason})"
            ),
            is_error=True,
            error_type="runtime_error",
        )

    # 构造子 Agent 的注册表：四个常用内置工具 + 下一层派生与结果查询工具
    def _build_child_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in (ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool()):
            registry.register(tool)
        registry.register(
            SpawnAgentTool(
                provider=self._provider,
                permission_manager=self._permission_manager,
                max_steps=self._max_steps,
                session_id=self._session_id,
                task_registry=self._task_registry,
                depth=self._depth + 1,
            )
        )
        registry.register(AgentResultTool(self._task_registry))
        return registry
