from __future__ import annotations

import asyncio
import platform
from pathlib import Path

import pytest

from weavecode.core.context import ExecutionContext


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = dict(run_id="r1", goal="test goal", max_steps=5)
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# 功能：验证全局与项目规则同时存在时都出现在 system prompt 中且顺序正确
# 设计：分别设置 global_context 与 project_context，断言两段标题、内容，以及冲突裁决句
def test_both_rule_layers_present() -> None:
    ctx = _make_ctx(
        global_context="global line",
        project_context="project line",
    )
    prompt = ctx.system_prompt("BASE")
    assert "BASE" in prompt
    assert "## Global Rules (AGENTS.md)\nglobal line" in prompt
    assert "## Project Rules (AGENTS.md)\nproject line" in prompt
    # 顺序：global 在 project 之前
    assert prompt.index("Global Rules") < prompt.index("Project Rules")
    # 两份同时存在时才声明冲突裁决规则
    assert "the project rule wins" in prompt


# 功能：验证规则层均为空时 system prompt 只含 base + skill 清单段
# 设计：不设置任何规则字段，断言输出以 base 开头、含 skill 段，且无规则段
def test_no_rule_layers() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE_ONLY")
    assert prompt.startswith("BASE_ONLY")
    assert "## Skills" in prompt
    assert "Global Rules" not in prompt
    assert "Project Rules" not in prompt


# 功能：验证只有全局规则时只出现 Global 段，且不声明冲突裁决
# 设计：只设置 global_context，断言 Project 标题与裁决句都不出现（只有一份时无从冲突）
def test_only_global_rules() -> None:
    ctx = _make_ctx(global_context="global content")
    prompt = ctx.system_prompt("BASE")
    assert "## Global Rules (AGENTS.md)" in prompt
    assert "## Project Rules" not in prompt
    assert "the project rule wins" not in prompt


# 功能：验证只有项目规则时只出现 Project 段，且不声明冲突裁决
# 设计：只设置 project_context，断言 Global 标题与裁决句都不出现
def test_only_project_rules() -> None:
    ctx = _make_ctx(project_context="project content")
    prompt = ctx.system_prompt("BASE")
    assert "## Project Rules (AGENTS.md)" in prompt
    assert "## Global Rules" not in prompt
    assert "the project rule wins" not in prompt


# 功能：验证 system prompt 始终注入 <env> 环境块（工作目录 + 系统类型）
# 设计：断言 <env> 块存在且 Working directory 与当前 cwd、Platform 与 platform.system() 一致，确认模型能感知运行环境
def test_env_block_injected() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE")
    assert "<env>" in prompt and "</env>" in prompt
    assert f"Working directory: {Path.cwd()}" in prompt
    assert f"Platform: {platform.system()}" in prompt
    # env 块紧跟 base，先于记忆层
    assert prompt.index("<env>") < prompt.index("## Skills")


# 功能：异步版 system prompt 会真的让出事件循环（skill 清单扫盘已放进线程）
# 设计：用「哨兵任务是否已被调度」判定，而不是测耗时——若扫盘仍在主线程同步跑，
#       哨兵任务根本没机会执行，断言必失败。这是确定性的，不受机器快慢影响
@pytest.mark.asyncio
async def test_system_prompt_async_yields_to_event_loop() -> None:
    ctx = _make_ctx()

    async def sentinel() -> None:
        return

    task = asyncio.create_task(sentinel())
    prompt = await ctx.system_prompt_async("BASE")

    assert task.done(), "system_prompt_async 全程没有让出事件循环（skill 扫盘没进线程）"
    assert "## Skills" in prompt


# 功能：显式传入 skill_catalog 时不再扫盘，直接用传入的清单
# 设计：传一个哨兵字符串并断言它出现在 prompt 里——锁住「预渲染清单」这条入口（loop 走的就是它）
def test_system_prompt_accepts_prerendered_catalog() -> None:
    ctx = _make_ctx()

    prompt = ctx.system_prompt("BASE", skill_catalog="- myskill: desc (file: /x/SKILL.md)")

    assert "- myskill: desc (file: /x/SKILL.md)" in prompt
