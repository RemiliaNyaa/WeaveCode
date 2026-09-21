from __future__ import annotations

import asyncio

import pytest

from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome


# 登记一个后台子 Agent 任务：延迟 delay 秒后把结果写进登记表
def _add_running(
    runs: BackgroundRuns, run_id: str, delay: float = 0.0
) -> asyncio.Task[None]:
    async def _child() -> None:
        await asyncio.sleep(delay)
        runs.finish(run_id, SubagentOutcome(run_id, "success", f"{run_id}-done"))

    task = asyncio.create_task(_child())
    runs.add(run_id, task)
    return task


# 功能：未完成的 run 出现在 pending_ids，收账后消失
# 设计：pending_ids 是收工兜底"还有没有在跑的"唯一依据，冒烟覆盖记账→收账两次状态转换
@pytest.mark.asyncio
async def test_pending_ids_tracks_completion() -> None:
    runs = BackgroundRuns()
    _add_running(runs, "r1")

    assert runs.pending_ids() == ["r1"]
    assert runs.known_ids() == ["r1"]
    assert runs.outcome("r1") is None

    await runs.wait(None)

    assert runs.pending_ids() == []
    assert runs.outcome("r1") == SubagentOutcome("r1", "success", "r1-done")


# 功能：wait(None) 覆盖全部已登记的 run，wait([id]) 只等指定那几个
# 设计：登记两个 run 只等一个；再断言 wait(None) 把**已完成的那个也一并返回**——
#       这正是「先跑完、后收账」不能丢结果的保证
@pytest.mark.asyncio
async def test_wait_all_vs_selected() -> None:
    runs = BackgroundRuns()
    _add_running(runs, "r1")
    _add_running(runs, "r2")

    selected = await runs.wait(["r1"])
    assert [o.run_id for o in selected] == ["r1"]

    rest = await runs.wait(None)
    assert sorted(o.run_id for o in rest) == ["r1", "r2"]


# 功能：cancel_all 取消所有仍在跑的后台子 Agent，并让 pending 清空
# 设计：子任务用长 sleep 模拟卡住的场景；同时断言补记了 cancelled 结果，
#       因为收工兜底靠 pending 判断，残留一条会让 run 永远结不了束
@pytest.mark.asyncio
async def test_cancel_all_cancels_pending() -> None:
    runs = BackgroundRuns()
    task = _add_running(runs, "r1", delay=30)

    await runs.cancel_all()

    assert task.cancelled()
    assert runs.pending_ids() == []
    outcome = runs.outcome("r1")
    assert outcome is not None
    assert outcome.status == "failed"
    assert outcome.result == "cancelled"


# 功能：已完成的 run 不受 cancel_all 影响（不会被改写成 cancelled）
# 设计：边界检查——收尾清理只能动"还在跑的"，否则会把已拿到的好结果覆盖掉
@pytest.mark.asyncio
async def test_cancel_all_keeps_finished() -> None:
    runs = BackgroundRuns()
    _add_running(runs, "r1")
    await runs.wait(None)

    await runs.cancel_all()

    outcome = runs.outcome("r1")
    assert outcome is not None
    assert outcome.result == "r1-done"
