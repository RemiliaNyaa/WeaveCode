from __future__ import annotations

import platform
from pathlib import Path

from weavecode.core.context import ExecutionContext


def _make_ctx(**kwargs) -> ExecutionContext:
    defaults = dict(run_id="r1", goal="test goal", max_steps=5)
    defaults.update(kwargs)
    return ExecutionContext(**defaults)


# 功能：验证规则层均为空时 system prompt 只含 base + skill 清单段
# 设计：不设置任何规则字段，断言输出以 base 开头、含 skill 段，且无规则段
def test_no_rule_layers() -> None:
    ctx = _make_ctx()
    prompt = ctx.system_prompt("BASE_ONLY")
    assert prompt.startswith("BASE_ONLY")
    assert "## Skills" in prompt
    assert "Global Rules" not in prompt
    assert "Project Rules" not in prompt


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
