from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from weavecode.core.bus.events import RunFinishedEvent, RunStartedEvent
from weavecode.core.config import WeaveConfig
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus, EventHandler
from weavecode.core.events.writer import EventWriter
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.provider import AnthropicProvider
from weavecode.core.loop import AgentLoop
from weavecode.core.runs import RUNS_DIR, new_run_id
from weavecode.core.session.model import Session
from weavecode.core.session.store import SessionStore
from weavecode.core.task import TaskManager
from weavecode.core.trace.provider import TracingProvider
from weavecode.core.trace.writer import TraceWriter
from weavecode.core.tools.builtin import (
    BashTool,
    ListDirTool,
    NoteSaveTool,
    ReadFileTool,
    TaskCreateTool,
    TaskGetTool,
    TaskListTool,
    TaskUpdateTool,
    WriteFileTool,
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
        runs_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._trace = trace
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
    ) -> RunOutcome:
        run_id = run_id or new_run_id()
        # 有会话时读回整段历史并挂到会话的运行目录下，否则从 goal 起一份全新历史
        if session is not None and store is not None:
            run_path = store.runs_dir(session.id) / run_id
            history = store.read_messages(session.id)
            notes = store.read_notes(session.id)
        else:
            run_path = self._runs_dir / run_id
            history = [{"role": "user", "content": goal}]
            notes = ""
        run_path.mkdir(parents=True, exist_ok=True)

        # 建立事件总线，订阅调用方传进来的监听者
        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 工作记忆在这里建立：完整历史回放，goal 只在没有历史时作为第一条消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
            prefill_messages=history,
            session_notes=notes,
        )

        # 本次运行的任务存储：放在 run 目录下，同一次 run 内的工具共享同一份状态
        task_manager = TaskManager(run_path / ".tasks")
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
                    task_manager, run_id=run_id, session=session, store=store
                )
                loop = AgentLoop(
                    provider, registry, bus, tasks=task_manager, session_id=session_id
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

    # 构建本次运行的工具注册表：内置文件工具 + 任务工具（共用同一个任务存储）
    def _build_registry(
        self,
        task_manager: TaskManager,
        *,
        run_id: str | None = None,
        session: Session | None = None,
        store: SessionStore | None = None,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        for t in [ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool()]:
            registry.register(t)
        for t in [
            TaskCreateTool(task_manager),
            TaskUpdateTool(task_manager),
            TaskListTool(task_manager),
            TaskGetTool(task_manager),
        ]:
            registry.register(t)
        # 笔记工具只在 session run 里注册：没有 session 就没有写入目标
        if session is not None and store is not None and run_id is not None:
            registry.register(NoteSaveTool(store, session.id, run_id))
        return registry
