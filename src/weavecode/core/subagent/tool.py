from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from weavecode.core.bus.events import SubagentFinishedEvent, SubagentStartedEvent
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.events.writer import EventWriter
from weavecode.core.loop import SPAWN_AGENT_TOOL_NAME, AgentLoop
from weavecode.core.runs import new_run_id
from weavecode.core.storage.database import Database
from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.llm.base import LLMProvider
    from weavecode.core.permissions.manager import PermissionManager


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SpawnAgentParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str
    prompt: str
    # 后台模式：True 时立即返回 run_id，主 Agent 之后用 wait_agent 收结果
    background: bool = False


# 在隔离的冷启动上下文中派生子 agent；默认阻塞等结果，background=True 时后台跑
class SpawnAgentTool(BaseTool):
    name = SPAWN_AGENT_TOOL_NAME
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
        "- Set background=true to run the sub-agent in the background instead: the "
        "call returns immediately with a run_id, and you collect the result later with "
        "wait_agent. Use this when you have other work to do while the sub-agent runs. "
        "You MUST collect every background sub-agent with wait_agent before ending "
        "your turn.\n"
        "- background is decided PER STEP, not per call: if one step contains several "
        "spawn_agent calls and even ONE of them is blocking (including the default), "
        "then ALL of them run blocking in that step. To get background execution, set "
        "background=true on EVERY spawn_agent call in that step. (All tool calls in a "
        "step run concurrently and the step never returns early, so background gains "
        "you nothing while a blocking call is present.)\n"
        "- To run several sub-agents in parallel, issue MULTIPLE spawn_agent tool "
        "calls in a single tool-calling step (one call per sub-task). The system "
        "executes them concurrently. Do NOT wait for one to finish before issuing "
        "the next.\n"
        "- The sub-agent starts with a clean context containing ONLY the provided "
        "prompt - it does NOT inherit the current conversation history. Be explicit "
        "and self-contained in prompt.\n"
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
            "background": {
                "type": "boolean",
                "description": (
                    "Run the sub-agent in the background and return a run_id immediately "
                    "(collect the result later with wait_agent). Defaults to false "
                    "(blocking). Decided per step: if any spawn_agent call in the same "
                    "step is blocking, all of them block."
                ),
            },
        },
        "required": ["description", "prompt"],
    }
    params_model = SpawnAgentParams

    # 构造 SpawnAgentTool（只由主 Agent 使用；子 Agent 不再拥有此工具，故无嵌套层级）
    #
    # 子 Agent 的工具集由 child_tools 工厂决定 —— 主 Agent 与子 Agent 共用同一份工具定义，
    # 唯一差别是计划存储（主 FilePlanStorage / 子 NoopPlanStorage），从根上避免两处清单打架。
    def __init__(
        self,
        provider: LLMProvider,
        parent_bus: EventBus,
        parent_run_id: str,
        permission_manager: PermissionManager | None,
        max_steps: int,
        session_id: str,
        working_dir: str = "",
        *,
        child_tools: Callable[[], list[BaseTool]],
        mcp_tools: Sequence[BaseTool] | None = None,
        runs: BackgroundRuns | None = None,
        db: Database | None = None,
    ) -> None:
        self._provider = provider
        self._parent_bus = parent_bus
        self._parent_run_id = parent_run_id
        self._permission_manager = permission_manager
        self._max_steps = max_steps
        self._session_id = session_id
        self._working_dir = working_dir
        self._child_tools = child_tools
        self._mcp_tools = list(mcp_tools or [])
        self._runs = runs
        self._db = db

    # 派生子 agent：默认阻塞等结果；background=True 时起后台任务并立即返回 run_id
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = SpawnAgentParams.model_validate(params)
        child_run_id = new_run_id()

        if p.background and self._runs is not None:
            task = asyncio.create_task(self._run_background(child_run_id, p))
            self._runs.add(child_run_id, task)
            return ToolResult(
                content=(
                    f"Sub-agent started in the background.\n"
                    f"run_id: {child_run_id}\n"
                    f"description: {p.description}\n"
                    "It has NOT finished yet. Call wait_agent (omit run_ids to wait for "
                    "all of them) to collect its result - you must do so before ending "
                    "your turn."
                )
            )

        return await self._run_child(child_run_id, p)

    # 后台入口：把子 Agent 跑完并把结果记进登记表；被取消时也记一条 failed
    async def _run_background(self, child_run_id: str, p: SpawnAgentParams) -> None:
        runs = self._runs
        assert runs is not None  # 只有登记表存在时才会走后台分支
        try:
            result = await self._run_child(child_run_id, p)
        except asyncio.CancelledError:
            runs.finish(child_run_id, SubagentOutcome(child_run_id, "failed", "cancelled"))
            raise
        status = "failed" if result.is_error else "success"
        runs.finish(child_run_id, SubagentOutcome(child_run_id, status, result.content))

    # 跑一个子 Agent：冷启动上下文 + 事件桥接 + 写事件文件，返回其结果
    async def _run_child(self, child_run_id: str, p: SpawnAgentParams) -> ToolResult:
        child_context = ExecutionContext(
            run_id=child_run_id,
            goal=p.prompt,
            max_steps=self._max_steps,
            working_dir=self._working_dir,
        )

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
                description=p.description,
                ts=_now(),
            )
        )

        # 结束事件放在 finally：成功、失败、被取消都要发，否则 TUI 的「进行中」状态会泄漏
        try:
            async with EventWriter(self._db, child_run_id) as writer:
                writer.subscribe(child_bus)
                await child_loop.run(child_context)
        finally:
            status = child_context.status if child_context.status != "running" else "failed"
            await self._parent_bus.publish(
                SubagentFinishedEvent(
                    run_id=child_run_id,
                    parent_run_id=self._parent_run_id,
                    status=status,
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

    # 构造子 Agent 的 registry：与主 Agent 同一份工具定义（唯一差别是 NoopPlanStorage）
    # + MCP 工具；不含 spawn_agent，因此子 Agent 无法再派生
    def _build_child_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for tool in self._child_tools():
            registry.register(tool)
        for mcp_tool in self._mcp_tools:
            registry.register(mcp_tool)
        return registry
