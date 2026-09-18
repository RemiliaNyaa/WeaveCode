from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from weavecode.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.events.writer import EventWriter
from weavecode.core.loop import AgentLoop
from weavecode.core.runs import new_run_id
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.llm.base import LLMProvider
    from weavecode.core.permissions.manager import PermissionManager


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    description: str
    prompt: str


# 在隔离的冷启动上下文中派生子 agent，恒阻塞等结果
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
        "- This tool BLOCKS: it waits for the sub-agent to finish, and the result comes "
        "back inline as the tool output.\n"
        "- To run several sub-agents in parallel, issue MULTIPLE spawn_agent tool calls "
        "in a single tool-calling step (one call per sub-task). The system executes them "
        "concurrently. Do NOT wait for one to finish before issuing the next.\n"
        "- The sub-agent starts with a clean context containing ONLY the provided prompt "
        "- it does NOT inherit the current conversation history. Be explicit and "
        "self-contained in prompt.\n"
        "- The sub-agent has the same tools as you, except it cannot spawn further "
        "sub-agents (no nesting).\n"
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
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造派生工具：只有主 Agent 持有它，层级固定为主、子两级
    #
    # 子 Agent 的工具集由 child_tools 工厂决定 —— 主 Agent 与子 Agent 共用同一份工具定义，
    # 唯一差别是计划存储（主落盘、子不落盘），从根上避免两处清单打架。
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        session_id: str,
        runs_dir: Path,
        working_dir: str = "",
        *,
        child_tools: Callable[[], list[BaseTool]],
        mcp_tools: Sequence[BaseTool] | None = None,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._session_id = session_id
        self._runs_dir = runs_dir
        self._working_dir = working_dir
        self._child_tools = child_tools
        self._mcp_tools = list(mcp_tools or [])

    # 派生子 agent 并等它跑完，结果作为工具输出返回
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)
        child_run_id = new_run_id()
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            working_dir=self._working_dir,
        )
        return await self._run_child(child_run_id, child_context, p)

    # 跑一个子 Agent：冷启动上下文 + 事件桥接 + 写事件文件，返回它的结果
    async def _run_child(
        self, child_run_id: str, child_context: ExecutionContext, p: SpawnAgentParams
    ) -> ToolResult:
        child_bus = EventBus()

        # 将子 bus 所有事件桥接到父 bus，TUI 据此渲染嵌套进度
        async def _bridge(event: BaseModel) -> None:
            await self._parent_bus.publish(event)

        child_bus.subscribe(_bridge)

        child_registry = self._build_child_registry()
        child_loop = AgentLoop(
            self._provider,
            child_registry,
            child_bus,
            permission_manager=self._permission_manager,
            session_id=self._session_id,
            working_dir=self._working_dir,
        )

        await self._parent_bus.publish(
            SubagentStartedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                ts=_now(),
            )
        )

        async with EventWriter(self._runs_dir / child_run_id / "events.jsonl") as writer:
            writer.subscribe(child_bus)
            await child_loop.run(child_context)

        await self._parent_bus.publish(
            SubagentFinishedEvent(
                run_id=child_run_id,
                parent_run_id=self._parent_run_id,
                ts=_now(),
            )
        )

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

    # 构造子 Agent 的 registry：与主 Agent 同一份工具定义 + MCP 工具；不含 spawn_agent
    def _build_child_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in self._child_tools():
            registry.register(tool)
        for mcp_tool in self._mcp_tools:
            registry.register(mcp_tool)
        return registry
