from __future__ import annotations

import asyncio

import pytest

from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome
from weavecode.core.subagent.wait_tool import WaitAgentTool


# 登记一个已经拿到结果的后台 run（占位任务立即跑完，避免留下 pending 任务噪音）
async def _finish(runs: BackgroundRuns, run_id: str) -> None:
    runs.add(run_id, asyncio.create_task(asyncio.sleep(0)))
    await asyncio.sleep(0)
    runs.finish(run_id, SubagentOutcome(run_id, "success", f"{run_id}-out"))


# 功能：没有任何后台子 Agent 时，wait_agent 直接返回提示而不是阻塞
# 设计：空登记表是最常见的「模型多此一举」场景，断言不抛异常且给出可读提示
@pytest.mark.asyncio
async def test_no_pending_returns_notice() -> None:
    tool = WaitAgentTool(BackgroundRuns())

    result = await tool.invoke({})

    assert not result.is_error
    assert "No background sub-agents" in result.content


# 功能：省略 run_ids 时收齐全部后台子 Agent 的结果（含已完成的）
# 设计：两个 run 都已结束时调 wait_agent——必须仍能取到结果，否则「干完自己的活再收账」会丢结果
@pytest.mark.asyncio
async def test_wait_all_returns_every_result() -> None:
    runs = BackgroundRuns()
    await _finish(runs, "r1")
    await _finish(runs, "r2")
    tool = WaitAgentTool(runs)

    result = await tool.invoke({})

    assert "r1-out" in result.content
    assert "r2-out" in result.content


# 功能：传入 run_ids 时只返回指定那几个 run 的结果
# 设计：只要一个，断言另一个的文本**不**出现——锁住「指定收账」而不是「顺手全收」
@pytest.mark.asyncio
async def test_wait_selected_only() -> None:
    runs = BackgroundRuns()
    await _finish(runs, "r1")
    await _finish(runs, "r2")
    tool = WaitAgentTool(runs)

    result = await tool.invoke({"run_ids": ["r1"]})

    assert "r1-out" in result.content
    assert "r2-out" not in result.content


# 功能：run_ids 指向不存在的 run 时回填 is_error，而不是静默成功
# 设计：模型可能凭记忆编 run_id；必须让它看见错误才能自我纠正，否则会以为已经收齐
@pytest.mark.asyncio
async def test_unknown_run_id_is_error() -> None:
    runs = BackgroundRuns()
    await _finish(runs, "r1")
    tool = WaitAgentTool(runs)

    result = await tool.invoke({"run_ids": ["nope"]})

    assert result.is_error
    assert "r1" in result.content  # 把已知 run_id 回给模型，方便它改用正确的
