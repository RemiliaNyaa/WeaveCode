from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse
from weavecode.core.loop import AgentLoop
from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome
from weavecode.core.tools.registry import ToolRegistry


# 按脚本逐次返回 end_turn 的假 provider；可在第 N 次调用时触发一个钩子
class _ScriptedProvider:
    def __init__(self, texts: list[str], hooks: dict[int, Callable[[], None]] | None = None) -> None:
        self._texts = texts
        self._hooks = hooks or {}
        self.calls = 0

    # 每次调用返回脚本里的下一段文字（用完后重复最后一段）
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.calls += 1
        hook = self._hooks.get(self.calls)
        if hook is not None:
            hook()
        return LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text=self._texts[min(self.calls, len(self._texts)) - 1],
        )


def _loop(provider: Any, runs: BackgroundRuns) -> AgentLoop:
    return AgentLoop(provider, ToolRegistry(), EventBus(), runs=runs)


def _context(max_steps: int = 6) -> ExecutionContext:
    return ExecutionContext(run_id="run-1", goal="do the thing", max_steps=max_steps)


# 功能：还有后台子 Agent 在跑时，模型发 end_turn 不能被接受，必须先收齐结果
# 设计：第一级「提醒」、第二级「系统代收」、第三步才真正收尾——用脚本化 provider 加钩子
#       把三级兜底变成确定性时序（钩子在第 2 次调用时放行子 Agent 完成），不依赖真实耗时
@pytest.mark.asyncio
async def test_end_turn_blocked_until_background_collected() -> None:
    runs = BackgroundRuns()
    ready = asyncio.Event()

    async def _child() -> None:
        await ready.wait()
        runs.finish("r1", SubagentOutcome("r1", "success", "bg-findings"))

    runs.add("r1", asyncio.create_task(_child()))
    provider = _ScriptedProvider(["draft", "still working", "final"], hooks={2: ready.set})
    context = _context()

    await _loop(provider, runs).run(context)

    assert context.status == "success"
    assert context.result == "final"
    assert provider.calls == 3

    transcript = "\n".join(str(m["content"]) for m in context.messages)
    assert "background sub-agent(s) are still running" in transcript  # 第一级提醒
    assert "bg-findings" in transcript  # 第二级系统代收把结果喂回模型
    assert "final" in transcript


# 功能：没有后台子 Agent 时 end_turn 立即结束本轮，不做任何兜底
# 设计：兜底的边界——避免「没有后台任务也被拦」；断言 provider 只被调用一次
@pytest.mark.asyncio
async def test_end_turn_immediate_without_background() -> None:
    provider = _ScriptedProvider(["done"])
    context = _context()

    await _loop(provider, BackgroundRuns()).run(context)

    assert context.status == "success"
    assert context.result == "done"
    assert provider.calls == 1


# 功能：不给 runs（子 Agent 的循环）时，end_turn 行为与改造前一致
# 设计：子 Agent 不能派发，`runs=None` 必须走老路径；防止兜底逻辑污染嵌套层级
@pytest.mark.asyncio
async def test_end_turn_without_runs_registry() -> None:
    provider = _ScriptedProvider(["child answer"])
    context = _context()

    await AgentLoop(provider, ToolRegistry(), EventBus()).run(context)

    assert context.status == "success"
    assert context.result == "child answer"
    assert provider.calls == 1
