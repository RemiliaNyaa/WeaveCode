from __future__ import annotations

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
