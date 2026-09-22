from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from weavecode.core.bus.events import RunFinishedEvent, RunStartedEvent
from weavecode.core.compact.compactor import Compactor
from weavecode.core.config import WeaveConfig
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus, EventHandler
from weavecode.core.events.writer import EventWriter
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.provider import AnthropicProvider
from weavecode.core.loop import AgentLoop
from weavecode.core.mcp.server import McpServerManager
from weavecode.core.memory.loader import load_rules_file
from weavecode.core.permissions.manager import PermissionManager
from weavecode.core.runs import RUNS_DIR, new_run_id
from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore
from weavecode.core.subagent.runs import BackgroundRuns
from weavecode.core.subagent.tool import SpawnAgentTool
from weavecode.core.subagent.wait_tool import WaitAgentTool
from weavecode.core.tools.base import BaseTool
from weavecode.core.trace.provider import TracingProvider
from weavecode.core.trace.writer import TraceWriter
from weavecode.core.tools.builtin import (
    BashTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from weavecode.core.tools.builtin.update_plan import (
    FilePlanStorage,
    NoopPlanStorage,
    PlanStorage,
    UpdatePlanTool,
)
from weavecode.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class RunOutcome:
    status: str
    result: str
    reason: str | None


class AgentRunner:
    # 组装所有运行时依赖，准备执行一次完整的 agent run
    def __init__(
        self,
        config: WeaveConfig,
        *,
        bus: EventBus | None = None,
        provider: LLMProvider | None = None,
        extra_handlers: list[EventHandler] | None = None,
        trace: TraceWriter | None = None,
        permission_manager: PermissionManager | None = None,
        mcp_manager: McpServerManager | None = None,
        runs_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._trace = trace
        self._permission_manager = permission_manager
        self._mcp_manager = mcp_manager
        self._runs_dir = runs_dir if runs_dir is not None else RUNS_DIR

    # 执行一次 agent run（委托给 run_and_capture）
    async def run(self, goal: str, *, run_id: str | None = None) -> RunOutcome:
        return await self.run_and_capture(goal, run_id=run_id)

    # 执行 agent run 并返回 RunOutcome（含最终文字结果）
    async def run_and_capture(
        self,
        goal: str,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
        plan_storage: PlanStorage | None = None,
        working_dir: str | None = None,
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        # 工作目录优先级：显式参数 > session 自带 > 进程 cwd
        if working_dir is None:
            working_dir = (
                session.effective_working_dir() if session is not None else str(Path.cwd())
            )
        # 会话与消息已入库：运行目录只剩事件文件与计划文件两个用途
        run_path = self._runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)

        # 恢复历史改成就地等待落库的异步读取；无会话时从 goal 起一份全新历史
        if session is not None and store is not None:
            history = await store.read_messages(session.id)
        else:
            history = [{"role": "user", "content": goal}]

        # 建立事件总线，订阅调用方传进来的监听者
        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 规则文件：全局 ~/.weave/AGENTS.md + 项目 <working_dir>/.weave/AGENTS.md
        global_ctx = load_rules_file(Path("~/.weave/AGENTS.md").expanduser())
        project_ctx = load_rules_file(Path(working_dir) / ".weave" / "AGENTS.md")

        # 工作记忆在这里建立：完整历史回放，goal 只在没有历史时作为第一条消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            global_context=global_ctx,
            project_context=project_ctx,
            working_dir=working_dir,
        )

        # 计划存储：调用方注入时跨 run 复用同一份；否则全量覆盖写进会话的任务目录
        if plan_storage is None:
            tasks_dir = run_path / ".tasks"
            if session is not None:
                tasks_dir = Path("~/.weave/sessions").expanduser() / session.id / ".tasks"
            plan_storage = FilePlanStorage(tasks_dir)
        session_id = session.id if session is not None else ""

        # 后台子 Agent 登记表：wait_agent 与收工兜底都靠它，整个 run 共用一份
        runs = BackgroundRuns()

        # 事件文件用 async with 打开：无论正常结束、报错还是被中断都会正确关闭
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                provider: LLMProvider = self._provider or AnthropicProvider(
                    self._config.llm.default_model
                )
                # 有 trace 时在外层包一层：LLM 的请求与响应往返都写进 trace 文件
                if self._trace is not None:
                    provider = TracingProvider(
                        provider,
                        self._trace,
                        include_payload=self._config.trace.include_llm_payload,
                    )
                registry = self._build_registry(
                    plan_storage,
                    run_id=run_id,
                    provider=provider,
                    bus=bus,
                    session_id=session_id,
                    working_dir=working_dir,
                    runs=runs,
                )
                compactor = Compactor(bus, session_id=session_id)
                loop = AgentLoop(
                    provider,
                    registry,
                    bus,
                    session_id=session_id,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
                    auto_compact=self._config.compaction.auto,
                    reserve_tokens=self._config.compaction.reserve_tokens,
                    model=self._config.llm.default_model,
                    working_dir=working_dir,
                    runs=runs,
                    repeat_limit=self._config.agent.repeat_limit,
                )
                await loop.run(context)
            except asyncio.CancelledError:
                cancelled = True
                if not context.is_done():
                    context.mark_failed("cancelled")
            except Exception:
                log.exception("agent run failed run_id=%s step=%d", run_id, context.step)
                if not context.is_done():
                    context.mark_failed("llm_error")
            finally:
                # run 收尾：取消仍在跑的后台子 Agent，避免它们继续消耗 token
                # （用户 ESC 中止 / LLM 错误 / 超步数都会走到这里）
                with suppress(asyncio.CancelledError):
                    await runs.cancel_all()

            # 结束事件在所有情况下都发布，保证事件文件里永远有头有尾
            await bus.publish(
                RunFinishedEvent(
                    run_id=run_id,
                    status=context.status,
                    reason=context.reason,
                    steps=context.step,
                    ts=_now(),
                )
            )

        if cancelled:
            # EventWriter 已关闭，现在才能把取消信号传给上层
            raise asyncio.CancelledError()

        return RunOutcome(
            status=context.status,
            result=context.result,
            reason=context.reason,
        )

    # 返回一个 agent 可用的内置工具清单（主 Agent 与子 Agent 共用同一份定义）
    # 唯一差别由 plan_storage 决定：主 Agent 落盘、子 Agent 用 NoopPlanStorage
    def _builtin_tools(self, plan_storage: PlanStorage) -> list[BaseTool]:
        return [
            ReadFileTool(),
            BashTool(),
            WriteFileTool(),
            ListDirTool(),
            GlobTool(),
            GrepTool(),
            UpdatePlanTool(plan_storage),
        ]

    # 构建本次运行的工具注册表：内置工具 + 外部工具服务器的工具
    def _build_registry(
        self,
        plan_storage: PlanStorage,
        *,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        session_id: str = "",
        working_dir: str = "",
        runs: BackgroundRuns | None = None,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for t in self._builtin_tools(plan_storage):
            registry.register(t)

        mcp_tools = self._mcp_manager.get_tools() if self._mcp_manager is not None else []

        # 子 Agent 相关工具只给主 Agent：spawn_agent 派生、wait_agent 收后台结果
        if provider is not None and bus is not None and run_id is not None and runs is not None:
            registry.register(
                SpawnAgentTool(
                    provider=provider,
                    parent_bus=bus,
                    parent_run_id=run_id,
                    permission_manager=self._permission_manager,
                    max_steps=self._config.agent.max_steps,
                    session_id=session_id,
                    working_dir=working_dir,
                    runs_dir=self._runs_dir,
                    # 子 Agent 拿到同一份内置工具（计划不落盘）+ MCP 工具，但不含 spawn_agent
                    child_tools=lambda: self._builtin_tools(NoopPlanStorage()),
                    mcp_tools=mcp_tools,
                    runs=runs,
                )
            )
            registry.register(WaitAgentTool(runs))

        for mcp_tool in mcp_tools:
            registry.register(mcp_tool)
        return registry
