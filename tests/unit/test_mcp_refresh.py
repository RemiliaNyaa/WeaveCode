from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from mcp.types import ToolListChangedNotification

from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, ToolCallBlock
from weavecode.core.loop import AgentLoop
from weavecode.core.mcp.client import _make_message_handler
from weavecode.core.mcp.server import McpServerManager
from weavecode.core.mcp.tool import McpTool
from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.builtin import ReadFileTool
from weavecode.core.tools.registry import ToolRegistry


@dataclass
class _FakeToolDef:
    # 冒充官方 SDK 的 Tool 对象：McpTool 只读 name / description / input_schema
    name: str
    description: str = "fake"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )


# 假的 MCP 会话句柄：list_tools 返回预设工具名，并记录被调用次数 / 可切换成失败
class _FakeHandle:
    def __init__(self, names: list[str]) -> None:
        self.names = names
        self.calls = 0
        self.fail = False

    # 返回预设工具定义；fail=True 时抛错（模拟 server 挂了 / 超时）
    async def list_tools(self) -> list[Any]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("server unavailable")
        return [_FakeToolDef(n) for n in self.names]


# 组装一个已"连上"若干 server 的 manager（直接塞句柄，跳过真实连接）
def _manager_with(handles: dict[str, _FakeHandle]) -> McpServerManager:
    mgr = McpServerManager()
    mgr._handles = dict(handles)  # type: ignore[assignment]
    mgr._tools = {name: [] for name in handles}
    return mgr


# 功能：只有「工具列表已变」通知才触发回调，其它通知不触发
# 设计：直接调 handler 并断言回调次数——这是「通知驱动」的唯一入口，必须先锁住它
@pytest.mark.asyncio
async def test_message_handler_fires_only_on_tool_list_changed() -> None:
    marks: list[int] = []
    handler = _make_message_handler(lambda: marks.append(1))

    await handler(ToolListChangedNotification(method="notifications/tools/list_changed"))
    assert marks == [1]

    await handler(object())  # 其它类型的通知（如日志消息）不该触发
    assert marks == [1]


# 功能：没有 on_tools_changed 回调时（配置关掉了刷新）handler 什么都不做
# 设计：传 None 调 handler 不应抛异常——对应 mcp.refreshOnNotify=false 的场景
@pytest.mark.asyncio
async def test_message_handler_without_callback_is_noop() -> None:
    handler = _make_message_handler(None)

    await handler(ToolListChangedNotification(method="notifications/tools/list_changed"))


# 功能：没有脏 server 时 refresh_dirty 立即返回，且不向 server 发任何请求
# 设计：断言假句柄的 list_tools 调用次数为 0——「无脏则零 I/O」是缓存不失效的前提
@pytest.mark.asyncio
async def test_refresh_dirty_is_noop_when_clean() -> None:
    handle = _FakeHandle(["a"])
    mgr = _manager_with({"s1": handle})

    assert await mgr.refresh_dirty() is False
    assert handle.calls == 0


# 功能：标脏后 refresh_dirty 重拉该 server 的清单并覆盖缓存
# 设计：先标脏、再改假句柄返回的工具名，断言缓存里的工具名跟着变——锁住「重拉 → 覆盖缓存」这条链路
@pytest.mark.asyncio
async def test_refresh_dirty_updates_cache() -> None:
    handle = _FakeHandle(["old"])
    mgr = _manager_with({"s1": handle})
    mgr._dirty.add("s1")
    handle.names = ["new_a", "new_b"]

    assert await mgr.refresh_dirty() is True
    assert sorted(t.name for t in mgr.get_tools()) == ["s1__new_a", "s1__new_b"]


# 功能：重拉失败时保留旧缓存，不把工具丢掉
# 设计：让假句柄抛错，断言缓存内容与调用前完全一致——与四家的「原子刷新」做法一致
@pytest.mark.asyncio
async def test_refresh_dirty_keeps_cache_on_failure() -> None:
    handle = _FakeHandle(["old"])
    mgr = _manager_with({"s1": handle})
    mgr._tools["s1"] = [McpTool(handle, "s1", _FakeToolDef("old"))]
    mgr._dirty.add("s1")
    handle.fail = True

    assert await mgr.refresh_dirty() is False
    assert [t.name for t in mgr.get_tools()] == ["s1__old"]


# 功能：脏标记按 server 粒度——只重拉被标脏的那个 server
# 设计：两个 server 只标一个，断言另一个的 list_tools 调用次数为 0（不做无谓 round-trip）
@pytest.mark.asyncio
async def test_refresh_dirty_is_per_server() -> None:
    h1, h2 = _FakeHandle(["a"]), _FakeHandle(["b"])
    mgr = _manager_with({"s1": h1, "s2": h2})
    mgr._dirty.add("s1")

    await mgr.refresh_dirty()

    assert h1.calls == 1
    assert h2.calls == 0


# 功能：脏标记处理完就清空，紧接着再调不会重复重拉
# 设计：连调两次 refresh_dirty，断言 list_tools 只被调了一次——避免同一步内重复 round-trip
@pytest.mark.asyncio
async def test_refresh_dirty_clears_flag() -> None:
    handle = _FakeHandle(["a"])
    mgr = _manager_with({"s1": handle})
    mgr._dirty.add("s1")

    await mgr.refresh_dirty()
    await mgr.refresh_dirty()

    assert handle.calls == 1


# 固定返回 end_turn、并记录每步收到的工具名列表的假 provider
class _CapturingProvider:
    def __init__(self) -> None:
        self.tool_names: list[list[str]] = []
        self._step = 0

    # 第 1 步让模型调 probe 工具（走一轮工具执行），第 2 步收工
    async def chat(self, **kwargs: Any) -> LlmResponse:
        self.tool_names.append([str(t["name"]) for t in kwargs.get("tool_schemas") or []])
        self._step += 1
        if self._step == 1:
            return LlmResponse(
                stop_reason="tool_use",
                tool_calls=[ToolCallBlock(id="1", name="probe", input={})],
                text="",
            )
        return LlmResponse(stop_reason="end_turn", tool_calls=[], text="done")


# 计数的假工具：只用来让第一步有工具可调
class _ProbeTool(BaseTool):
    name = "probe"
    description = "fake probe"
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    async def invoke(self, params: dict[str, object]) -> ToolResult:
        return ToolResult(content="ok")


# 功能：AgentLoop 每步都调 registry_provider，且 provider 返回的工具变化会在**下一步**的请求里生效
# 设计：让 provider「第 2 次调用时多给一个 read_file」，断言两次请求的 tools 名单不同——
#       这是「MCP 工具变化在下一步生效」的端到端证据（也是 prompt caching 不变则命中的前提）
@pytest.mark.asyncio
async def test_loop_rebuilds_registry_each_step() -> None:
    provider = _CapturingProvider()
    calls = 0

    async def registry_provider() -> ToolRegistry:
        nonlocal calls
        calls += 1
        reg = ToolRegistry()
        reg.register(_ProbeTool())
        if calls >= 2:
            reg.register(ReadFileTool())
        return reg

    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(), registry_provider=registry_provider
    )
    context = ExecutionContext(run_id="r", goal="g", max_steps=5)

    await loop.run(context)

    assert calls == 2
    assert provider.tool_names == [["probe"], ["probe", "read_file"]]


# 功能：没给 registry_provider 时行为与改造前一致（registry 全程不变）
# 设计：不传 provider，断言每步的 tools 名单完全一样——保证这次改动不影响默认路径
@pytest.mark.asyncio
async def test_loop_without_provider_keeps_single_registry() -> None:
    provider = _CapturingProvider()
    registry = ToolRegistry()
    registry.register(_ProbeTool())

    loop = AgentLoop(provider, registry, EventBus())
    context = ExecutionContext(run_id="r", goal="g", max_steps=5)

    await loop.run(context)

    assert provider.tool_names == [["probe"], ["probe"]]
