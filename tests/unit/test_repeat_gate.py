from __future__ import annotations

from typing import Any

import pytest

from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, ToolCallBlock
from weavecode.core.loop import AgentLoop, _call_signature, _step_signature
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.registry import ToolRegistry


# 计数的假工具：记录被**真正执行**了几次（被闸门拦下的调用不该走到这里）
class _CountingTool(BaseTool):
    name = "read_file"
    description = "fake read tool"
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    def __init__(self) -> None:
        self.calls = 0

    # 每次真正执行就 +1，返回固定成功结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        self.calls += 1
        return ToolResult(content="ok")


# 按脚本逐轮返回工具调用的假 provider；脚本用完后返回 end_turn
class _ToolLoopProvider:
    def __init__(self, calls_per_step: list[list[ToolCallBlock]]) -> None:
        self._script = calls_per_step
        self.steps = 0

    # 第 n 轮返回脚本里的第 n 组调用，脚本用完就收工
    async def chat(self, **kwargs: Any) -> LlmResponse:
        if self.steps < len(self._script):
            calls = self._script[self.steps]
            self.steps += 1
            return LlmResponse(stop_reason="tool_use", tool_calls=calls, text="")
        self.steps += 1
        return LlmResponse(stop_reason="end_turn", tool_calls=[], text="done")


# 造一个 read_file 调用块
def _call(tool_use_id: str, path: str) -> ToolCallBlock:
    return ToolCallBlock(id=tool_use_id, name="read_file", input={"path": path})


# 组装一个带假工具的 loop（repeat_limit 可调）
def _loop(provider: Any, registry: ToolRegistry, repeat_limit: int = 3) -> AgentLoop:
    return AgentLoop(provider, registry, EventBus(), repeat_limit=repeat_limit)


# 功能：单个调用的签名由「工具名 + 规范化参数」构成，键序不影响结果
# 设计：直接断言纯函数——签名是整条闸门的判定依据，键序敏感会让同一个调用被误判成两个
def test_call_signature_is_key_order_insensitive() -> None:
    a = ToolCallBlock(id="1", name="read_file", input={"path": "/a", "limit": 10})
    b = ToolCallBlock(id="2", name="read_file", input={"limit": 10, "path": "/a"})

    assert _call_signature(a) == _call_signature(b)


# 功能：一步的签名对同批调用做排序拼接，调用顺序不影响结果
# 设计：同轮多个调用是并发执行的、顺序无语义；顺序敏感会把同一批次判成不同批次
def test_step_signature_ignores_call_order() -> None:
    a = _call("1", "/a")
    b = _call("2", "/b")

    assert _step_signature([a, b]) == _step_signature([b, a])


# 功能：连续相同的调用在第 N 次被拦下（前 N-1 次正常执行）
# 设计：断言工具的**真实执行次数**——「拦下 = 不执行」是本设计的关键证据，只看结果文本验不出来
@pytest.mark.asyncio
async def test_repeat_blocked_at_limit() -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _ToolLoopProvider([[_call("1", "/a")], [_call("2", "/a")], [_call("3", "/a")]])
    context = ExecutionContext(run_id="r", goal="g", max_steps=10)

    await _loop(provider, registry).run(context)

    assert tool.calls == 2  # 第 3 次被拦，没有真正执行
    assert context.status == "success"


# 功能：连续拦截到第 3 次（即第 N+2 次重复）时强制终止 run，reason = repeat_loop
# 设计：把 N=3 的状态机整条走完，断言终止发生在第 5 次重复、且 reason 正确（TUI 靠它显示中断原因）
@pytest.mark.asyncio
async def test_repeat_terminates_after_max_hits() -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _ToolLoopProvider([[_call(str(i), "/a")] for i in range(1, 7)])
    context = ExecutionContext(run_id="r", goal="g", max_steps=10)

    await _loop(provider, registry).run(context)

    assert tool.calls == 2  # 只执行了前两次，后面全被拦
    assert context.status == "failed"
    assert context.reason == "repeat_loop"


# 功能：模型中途换路（参数不同）后计数替换清零，不被前面的重复拖累
# 设计：这是决策点 12 的不变式——A A A 之后改 B，B 必须从 1 重新开始计
@pytest.mark.asyncio
async def test_repeat_resets_when_model_changes_path() -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _ToolLoopProvider([
        [_call("1", "/a")], [_call("2", "/a")], [_call("3", "/b")],
        [_call("4", "/b")], [_call("5", "/b")], [_call("6", "/b")],
    ])
    context = ExecutionContext(run_id="r", goal="g", max_steps=10)

    await _loop(provider, registry).run(context)

    # /a 执行 2 次（第 3 次前换路）→ /b 执行 2 次（第 3 次被拦）→ 脚本走完收工
    assert tool.calls == 4
    assert context.status == "success"


# 功能：repeat_limit 调大可放宽闸门（配置生效）
# 设计：用 repeat_limit=5 跑 5 次相同调用，断言前 4 次都真正执行——锁住「阈值可配」这条
@pytest.mark.asyncio
async def test_repeat_limit_is_configurable() -> None:
    tool = _CountingTool()
    registry = ToolRegistry()
    registry.register(tool)
    provider = _ToolLoopProvider([[_call(str(i), "/a")] for i in range(1, 6)])
    context = ExecutionContext(run_id="r", goal="g", max_steps=10)

    await _loop(provider, registry, repeat_limit=5).run(context)

    assert tool.calls == 4
    assert context.status == "success"
