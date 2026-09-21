from __future__ import annotations

from weavecode.core.llm.types import ToolCallBlock
from weavecode.core.loop import SPAWN_AGENT_TOOL_NAME, _normalize_spawn_modes


def _spawn(tool_use_id: str, **params: object) -> ToolCallBlock:
    return ToolCallBlock(id=tool_use_id, name=SPAWN_AGENT_TOOL_NAME, input=dict(params))


# 功能：一步里只有一个 spawn_agent 时，它的 background 参数原样保留
# 设计：单独调用不存在「混用」；若被归一化误改成阻塞，非阻塞模式就基本不可用了
def test_single_spawn_untouched() -> None:
    calls = [_spawn("a", background=True)]

    _normalize_spawn_modes(calls)

    assert calls[0].input["background"] is True


# 功能：一步里只要有一个 spawn_agent 是阻塞（含默认不传），全部改成阻塞
# 设计：这是「方案 A」的核心不变式——同一轮不能同时出现内联结果和 run_id 两种返回形态
def test_mixed_step_forces_all_blocking() -> None:
    calls = [_spawn("a", background=True), _spawn("b")]

    _normalize_spawn_modes(calls)

    assert [c.input["background"] for c in calls] == [False, False]


# 功能：一步里全部显式 background=true 时保持非阻塞
# 设计：与上式互为边界，确保归一化不会把合法的「全后台并行」误判成阻塞
def test_all_background_kept() -> None:
    calls = [_spawn("a", background=True), _spawn("b", background=True)]

    _normalize_spawn_modes(calls)

    assert [c.input["background"] for c in calls] == [True, True]


# 功能：非 spawn_agent 的工具调用不被归一化改动
# 设计：归一化按工具名过滤，混入其它工具做边界检查，避免误改同轮的普通工具参数
def test_other_tools_untouched() -> None:
    other = ToolCallBlock(id="x", name="read_file", input={"path": "/tmp/a"})
    calls = [other, _spawn("a", background=True), _spawn("b")]

    _normalize_spawn_modes(calls)

    assert other.input == {"path": "/tmp/a"}
    assert [c.input["background"] for c in calls[1:]] == [False, False]
