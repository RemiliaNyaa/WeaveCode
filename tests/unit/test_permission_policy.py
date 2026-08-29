from __future__ import annotations

from weavecode.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    evaluate_hard,
    evaluate_soft,
    param_preview,
)

# ── 硬规则：黑名单 → 越界，未命中返回 None ─────────────────────────────────────


# 功能：验证黑名单在硬规则里最先命中，直接拒绝、不进软规则
# 设计：deny_patterns 属于"不可被任何用户选择覆盖"的一层，命中即 DENY
def test_hard_deny_pattern_wins() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW, deny_patterns=[r"rm\s+-rf"])
    assert evaluate_hard("bash", {"command": "rm -rf /tmp"}, policy) == PermissionDecision.DENY


# 功能：验证访问工作目录之外的命令被硬规则强制询问
# 设计：越界只对 bash 判定（路径藏在命令串里），干净命令硬规则不表态，交给软规则
def test_hard_outside_cwd_forces_ask() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate_hard("bash", {"command": "cat /etc/passwd"}, policy) == PermissionDecision.ASK
    assert evaluate_hard("bash", {"command": "ls -la"}, policy) is None


# 功能：验证非 bash 工具与缺少 command 字段的调用不走命令硬规则
# 设计：硬规则只看命令串，其余工具的路径越界由横切层负责，这里必须返回 None
def test_hard_skips_non_bash_tools() -> None:
    policy = ToolPolicy(default=PermissionDecision.ASK)
    assert evaluate_hard("write_file", {"path": "/tmp/x", "content": "y"}, policy) is None
    assert evaluate_hard("bash", {"args": "ls"}, policy) is None


# ── 软规则：白名单 → 默认值，可被用户缓存覆盖 ──────────────────────────────────


# 功能：验证白名单命中直接放行，未命中回工具默认值
# 设计：软规则不做任何"越界/危险"判断，只在名单与默认值之间二选一
def test_soft_allow_pattern_wins() -> None:
    policy = ToolPolicy(default=PermissionDecision.ASK, allow_patterns=[r"^ls "])
    assert evaluate_soft("bash", {"command": "ls -la"}, policy) == PermissionDecision.ALLOW
    assert evaluate_soft("bash", {"command": "cat x"}, policy) == PermissionDecision.ASK


# 功能：验证判定结果按传入策略的默认值返回
# 设计：软规则不读任何名单，只返回 default；不同工具可有不同默认
def test_soft_returns_tool_default() -> None:
    assert (
        evaluate_soft("bash", {"command": "ls -la"}, ToolPolicy(default=PermissionDecision.ALLOW))
        == PermissionDecision.ALLOW
    )
    assert (
        evaluate_soft("read_file", {"path": "x"}, ToolPolicy(default=PermissionDecision.ASK))
        == PermissionDecision.ASK
    )


# 功能：验证只读工具的默认策略是 ALLOW
# 设计：只读或安全工具默认不打扰用户，降低权限疲劳
def test_soft_safe_tools_default_allow() -> None:
    assert DEFAULT_POLICIES["read_file"].default == PermissionDecision.ALLOW
    assert DEFAULT_POLICIES["list_dir"].default == PermissionDecision.ALLOW


# ── param_preview ──────────────────────────────────────────────────────────────


# 功能：验证 param_preview 对已知工具返回 key='value' 格式的摘要
# 设计：TUI 审批卡片依赖这个摘要让用户快速理解工具要做什么，格式必须稳定
def test_param_preview_known_tools() -> None:
    assert param_preview("bash", {"command": "echo hi"}) == "command='echo hi'"
    assert param_preview("read_file", {"path": "README.md"}) == "path='README.md'"


# 功能：验证 param_preview 超出 60 字符时截断并加省略号
# 设计：避免审批卡片展示超长命令撑破 UI 布局
def test_param_preview_truncates_long_value() -> None:
    long_cmd = "echo " + "x" * 100
    preview = param_preview("bash", {"command": long_cmd})
    assert len(preview) <= 75  # key='<60 chars>…' overhead ~11 chars
    assert "…" in preview
