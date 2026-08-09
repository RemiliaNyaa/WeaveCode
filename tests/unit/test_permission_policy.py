from __future__ import annotations

from weavecode.core.permissions.policy import (
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    evaluate,
    param_preview,
)

# ── 静态判定：黑名单 → 越界 → 白名单 → 默认策略 ─────────────────────────────

# 功能：验证危险形态的命令被硬规则强制询问
# 设计：越界启发式命中「访问工作目录之外」的命令 → ASK；未命中任何名单时按默认策略处理
def test_hard_dangerous_command_forces_ask() -> None:
    policy = ToolPolicy(default=PermissionDecision.ALLOW)
    assert evaluate("bash", {"command": "rm -rf /tmp"}, policy) == PermissionDecision.ASK
    assert evaluate("bash", {"command": "echo hi > /etc/x"}, policy) == PermissionDecision.ASK
    assert evaluate("bash", {"command": "ls -la"}, policy) == PermissionDecision.ALLOW


# ── 默认策略 ─────────────────────────────────────────────────────────────────

# 功能：验证判定结果按传入策略的默认值返回
# 设计：软规则不读任何名单，只返回 default；未登记工具走 ASK 兜底
def test_soft_returns_tool_default() -> None:
    assert evaluate("bash", {"command": "ls -la"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW
    assert evaluate("bash", {"command": "cat x"}, ToolPolicy(default=PermissionDecision.ASK)) == PermissionDecision.ASK
    assert evaluate("read_file", {"path": "x"}, ToolPolicy(default=PermissionDecision.ALLOW)) == PermissionDecision.ALLOW


# 功能：验证只读工具的默认策略是 ALLOW
# 设计：只读或安全工具默认不打扰用户，降低权限疲劳
def test_soft_safe_tools_default_allow() -> None:
    assert evaluate("read_file", {"path": "README.md"}, DEFAULT_POLICIES["read_file"]) == PermissionDecision.ALLOW
    assert evaluate("list_dir", {"path": "."}, DEFAULT_POLICIES["list_dir"]) == PermissionDecision.ALLOW


# ── param_preview ────────────────────────────────────────────────────────────

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
