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
from weavecode.core.tools.builtin import ReadFileTool
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
        runs_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._bus = bus
        self._provider = provider
        self._extra_handlers: list[EventHandler] = extra_handlers or []
        self._runs_dir = runs_dir if runs_dir is not None else RUNS_DIR

    # 执行一次完整的 agent run，返回带结果的运行结局
    async def run(self, goal: str, *, run_id: str | None = None) -> RunOutcome:
        run_id = run_id or new_run_id()
        run_path = self._runs_dir / run_id
        run_path.mkdir(parents=True, exist_ok=True)

        # 建立事件总线，订阅调用方传进来的监听者
        bus = self._bus if self._bus is not None else EventBus()
        for h in self._extra_handlers:
            bus.subscribe(h)

        # 工作记忆在这里建立，goal 成为第一条消息
        context = ExecutionContext(
            run_id=run_id,
            goal=goal,
            max_steps=self._config.agent.max_steps,
        )

        # 事件文件用 async with 打开：无论正常结束、报错还是被中断都会正确关闭
        async with EventWriter(run_path / "events.jsonl") as writer:
            writer.subscribe(bus)
            await bus.publish(RunStartedEvent(run_id=run_id, goal=goal, ts=_now()))

            cancelled = False
            try:
                provider: LLMProvider = self._provider or AnthropicProvider(
                    self._config.llm.default_model
                )
                registry = ToolRegistry()
                registry.register(ReadFileTool())
                loop = AgentLoop(provider, registry, bus)
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
