from __future__ import annotations

import asyncio

import pytest

from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, ToolCallBlock
from weavecode.core.loop import AgentLoop
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.registry import ToolRegistry

# --- stubs -------------------------------------------------------------------

class _MockProvider:
    """Returns canned responses in order; raises exc immediately if given."""

    def __init__(
        self,
        responses: list[LlmResponse],
        exc: BaseException | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._exc = exc

    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        if self._exc is not None:
            raise self._exc
        return next(self._responses)

class _EchoTool(BaseTool):
    name = "echo"
    description = "Echoes msg"
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {"msg": {"type": "string"}},
        "required": ["msg"],
    }

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content=str(params["msg"]))

# --- helpers -----------------------------------------------------------------

def _ctx(max_steps: int = 5) -> ExecutionContext:
    return ExecutionContext(run_id="r1", goal="test goal", max_steps=max_steps)

def _tc(name: str = "echo", inp: dict[str, object] | None = None, uid: str = "t1") -> ToolCallBlock:
    return ToolCallBlock(id=uid, name=name, input=inp or {"msg": "hi"})

def _make_loop(
    provider: _MockProvider,
    registry: ToolRegistry | None = None,
    bus: EventBus | None = None,
) -> tuple[AgentLoop, EventBus]:
    b = bus or EventBus()
    return AgentLoop(provider, registry or ToolRegistry(), b), b  # type: ignore[arg-type]

# --- tests -------------------------------------------------------------------

# 功能：验证 LLM 返回 end_turn 时 loop 将 context 标记为 success
# 设计：单步 provider 直接返回 end_turn，最简正常路径，确认 loop 的基本终止逻辑
async def test_end_turn_marks_success() -> None:
    provider = _MockProvider([LlmResponse(stop_reason="end_turn", text="done")])
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 1


# 功能：验证达到 max_steps 时 loop 以 exceeded_max_steps 原因将 context 标记为 failed
# 设计：设置 max_steps=2 + 无限 tool_use provider，同时验证 step 数量和失败原因，确认计数器与终止逻辑联动正确

# 功能：验证达到 max_steps 时 loop 以 exceeded_max_steps 原因将 context 标记为 failed
# 设计：设置 max_steps=2 + 无限 tool_use provider，同时验证 step 数量和失败原因，确认计数器与终止逻辑联动正确
async def test_max_steps_marks_failed() -> None:
    tc = _tc("unknown", {})
    provider = _MockProvider([LlmResponse(stop_reason="tool_use", tool_calls=[tc])] * 10)
    loop, _ = _make_loop(provider)
    ctx = _ctx(max_steps=2)
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "exceeded_max_steps"
    assert ctx.step == 2


# 功能：验证"调工具 → end_turn"的两步路径最终标记为 success
# 设计：provider 返回 [tool_use, end_turn] 序列，注册真实 EchoTool，覆盖最常见的正常工作路径

# 功能：验证"调工具 → end_turn"的两步路径最终标记为 success
# 设计：provider 返回 [tool_use, end_turn] 序列，注册真实 EchoTool，覆盖最常见的正常工作路径
async def test_tool_use_then_end_turn_marks_success() -> None:
    provider = _MockProvider([
        LlmResponse(stop_reason="tool_use", tool_calls=[_tc()]),
        LlmResponse(stop_reason="end_turn", text="summary"),
    ])
    registry = ToolRegistry()
    registry.register(_EchoTool())
    loop, _ = _make_loop(provider, registry)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "success"
    assert ctx.step == 2


# 功能：验证工具结果按 Anthropic 格式（tool_result user 消息）追加到消息历史
# 设计：检查 messages[2]（tool_result 所在位置），断言 tool_use_id 和 content，确认 loop 正确调用了 context.add_tool_result

# 功能：验证收到 CancelledError 时 loop 将 context 标记为 cancelled 后继续上抛 CancelledError
# 设计：用 pytest.raises 捕获 CancelledError，同时检查 context.status，确认优雅退出行为：先记录状态，再传播取消信号
async def test_cancelled_error_marks_failed_and_reraises() -> None:
    provider = _MockProvider([], exc=asyncio.CancelledError())
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    with pytest.raises(asyncio.CancelledError):
        await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "cancelled"


# 功能：验证 LLM 调用异常被捕获并标记为 llm_error，不向上传播
# 设计：provider 抛 RuntimeError，确认 loop 不崩溃、context 状态为 failed/llm_error，异常被正确吸收

# 功能：验证 LLM 调用异常被捕获并标记为 llm_error，不向上传播
# 设计：provider 抛 RuntimeError，确认 loop 不崩溃、context 状态为 failed/llm_error，异常被正确吸收
async def test_llm_api_error_marks_failed() -> None:
    provider = _MockProvider([], exc=RuntimeError("api error"))
    loop, _ = _make_loop(provider)
    ctx = _ctx()
    await loop.run(ctx)
    assert ctx.status == "failed"
    assert ctx.reason == "llm_error"


# 功能：验证每个步骤都发布 step.started 和 step.finished 事件
# 设计：注入 bus + 事件收集器，检查事件类型集合，确认步骤级事件的可观测性（S2 TUI 依赖这两个事件显示进度）
