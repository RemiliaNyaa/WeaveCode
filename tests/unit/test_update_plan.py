from __future__ import annotations

import json
from pathlib import Path

import pytest

from weavecode.core.tools.builtin.update_plan import (
    FilePlanStorage,
    UpdatePlanTool,
)


# 读出 tasks.json 里的任务列表（保持文件内的书写顺序）
def _read_tasks(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))


# 功能：多次调用是全量覆盖，旧任务被替换
# 设计：先写两项再写一项，断言文件里只剩最新一项——整体覆写语义的直接证据
async def test_update_plan_overwrites(tmp_path: Path) -> None:
    tool = UpdatePlanTool(FilePlanStorage(tmp_path))
    await tool.invoke({"tasks": [{"step": "a", "status": "pending"}]})
    await tool.invoke({"tasks": [{"step": "b", "status": "completed"}]})
    assert _read_tasks(tmp_path) == [{"step": "b", "status": "completed"}]


# 功能：读回的先后顺序 = 传入列表的顺序
# 设计：写 3 项后读回，断言顺序一致——JSON 数组自带顺序，全量覆盖不能把它打乱
async def test_update_plan_preserves_order(tmp_path: Path) -> None:
    tool = UpdatePlanTool(FilePlanStorage(tmp_path))
    await tool.invoke(
        {
            "tasks": [
                {"step": "第一步", "status": "completed"},
                {"step": "第二步", "status": "in_progress"},
                {"step": "第三步", "status": "pending"},
            ]
        }
    )
    assert _read_tasks(tmp_path) == [
        {"step": "第一步", "status": "completed"},
        {"step": "第二步", "status": "in_progress"},
        {"step": "第三步", "status": "pending"},
    ]


# 功能：参数缺少 tasks 时抛 ValidationError（schema_error 触发）
# 设计：传空字典，预期 pydantic 拒绝缺必填字段
async def test_update_plan_missing_tasks() -> None:
    tool = UpdatePlanTool(FilePlanStorage(Path(".")))
    with pytest.raises(Exception):
        await tool.invoke({})


# 功能：UpdatePlanParams 忽略额外字段（extra="ignore"）
# 设计：多传无关字段，断言不报错且只取 tasks
async def test_update_plan_extra_ignored(tmp_path: Path) -> None:
    tool = UpdatePlanTool(FilePlanStorage(tmp_path))
    result = await tool.invoke(
        {"tasks": [{"step": "a", "status": "pending"}], "explanation": "why"}
    )
    assert not result.is_error
    assert result.content == "任务列表更新成功"
