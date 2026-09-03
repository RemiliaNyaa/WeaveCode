from __future__ import annotations

import asyncio
import logging
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
from weavecode.core.permissions.manager import PermissionManager
from weavecode.core.runs import RUNS_DIR, new_run_id
from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore
from weavecode.core.subagent.tool import SpawnAgentTool
from weavecode.core.trace.provider import TracingProvider
from weavecode.core.trace.writer import TraceWriter
from weavecode.core.tools.builtin import (
    BashTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from weavecode.core.tools.builtin.update_plan import (
    FilePlanStorage,
    PlanStorage,
    UpdatePlanTool,
)
from weavecode.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()

# 读取规则文件全文；文件不存在或读不到时返回空串
def _read_rules(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        # 有会话时读回整段历史并挂到会话的运行目录下，否则从 goal 起一份全新历史
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            session_dir = store.session_dir(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            session_dir = run_path
        run_path.mkdir(parents=True, exist_ok=True)

        # 建立事件总线，订阅调用方传进来的监听者
        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 记忆背景：全局与项目两级上下文文件，文件不存在或为空时按空串处理
        global_ctx = _read_rules(Path("~/.weave/context.md").expanduser())
        project_ctx = _read_rules(Path(".weave/context.md"))

        # 工作记忆在这里建立：完整历史回放，goal 只在没有历史时作为第一条消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            global_context=global_ctx,
            project_context=project_ctx,
        )

        # 计划存储：调用方注入时跨 run 复用同一份；否则全量覆盖写进本次会话的任务目录
        if plan_storage is None:
            plan_storage = FilePlanStorage(session_dir / ".tasks")
        session_id = session.id if session is not None else ""

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
                )
                compactor = Compactor(bus, session_dir=session_dir)
                loop = AgentLoop(
                    provider,
                    registry,
                    bus,
                    session_id=session_id,
                    permission_manager=self._permission_manager,
                    compactor=compactor,
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

    # 构建本次运行的工具注册表：内置文件工具 + 计划工具 + 外部工具服务器的工具
    def _build_registry(
        self,
        plan_storage: PlanStorage,
        *,
        run_id: str | None = None,
        provider: LLMProvider | None = None,
        bus: EventBus | None = None,
        session_id: str = "",
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for t in [ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool()]:
            registry.register(t)
        registry.register(UpdatePlanTool(plan_storage))

        mcp_tools = self._mcp_manager.get_tools() if self._mcp_manager is not None else []

        # 派生子 Agent 的工具只给主 Agent：子 Agent 的注册表不再登记派生工具
        if provider is not None and bus is not None and run_id is not None:
            registry.register(
                SpawnAgentTool(
                    provider=provider,
                    parent_bus=bus,
                    parent_run_id=run_id,
                    permission_manager=self._permission_manager,
                    max_steps=self._config.agent.max_steps,
                    session_id=session_id,
                    runs_dir=self._runs_dir,
                )
            )

        for mcp_tool in mcp_tools:
            registry.register(mcp_tool)
        return registry
