from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionResult:
    """一次权限判定的结果：是否放行、走了哪条决策、以及触发判定的权限名与资源。"""

    allowed: bool
    decision: str
    # 触发判定的权限名：横切越界为 "external_directory"，其余为工具名
    permission: str = ""
    # 资源：越界时为「被访问的目录」，工具级为 "*"；供回馈文案使用
    resource: str = ""


# 检测 bash 命令是否操作 cwd 之外路径：已改为 tree-sitter 解析（见 shell_paths.py），
# 并统一走 external_directory 横切权限；此处不再使用正则启发式。


@dataclass
class ToolPolicy:
    # 工具层策略：目前只有默认决策。
    # （bash 的危险命令黑名单是硬编码的，见 command_safety.py，不经此配置。）
    default: PermissionDecision


DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    # 工具层默认全部放行（对齐 opencode 的宽松设计）：
    # bash 的危险命令由「危险黑名单」硬规则拦截，路径越界由 external_directory 拦截，
    # 其余工具默认不打扰用户。
    "bash":        ToolPolicy(default=PermissionDecision.ALLOW),
    "write_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "edit_file":   ToolPolicy(default=PermissionDecision.ALLOW),
    "read_file":   ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":    ToolPolicy(default=PermissionDecision.ALLOW),
    "glob":        ToolPolicy(default=PermissionDecision.ALLOW),
    "grep":        ToolPolicy(default=PermissionDecision.ALLOW),
    "update_plan": ToolPolicy(default=PermissionDecision.ALLOW),
    "spawn_agent": ToolPolicy(default=PermissionDecision.ALLOW),
    "wait_agent":  ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未在 DEFAULT_POLICIES 中登记的工具（如 MCP 工具）的兜底策略：保守询问
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

# bash 参数中展示用的关键字段映射
_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "read_file":  "path",
    "write_file": "path",
    "edit_file":  "path",
    "list_dir":   "path",
    "glob":       "pattern",
    "grep":       "pattern",
}
_PREVIEW_MAX = 60


# 为权限审批事件生成人类可读的参数摘要
def param_preview(tool_name: str, params: dict[str, Any]) -> str:
    key = _PREVIEW_KEY.get(tool_name)
    if key and key in params:
        val = str(params[key])
        if len(val) > _PREVIEW_MAX:
            val = val[:_PREVIEW_MAX] + "…"
        return f"{key}={val!r}"
    snippet = str(params)
    return snippet[:_PREVIEW_MAX] if len(snippet) > _PREVIEW_MAX else snippet


# 硬规则：危险命令黑名单（硬编码，用户不可配置），不可被任何缓存绕过；未命中返回 None
def evaluate_hard(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy,
) -> PermissionDecision | None:
    if tool_name != "bash":
        return None  # 只有 bash 有命令内容，其余工具无硬规则

    command = str(params.get("command", ""))
    if not command:
        return None

    from weavecode.core.permissions.command_safety import is_dangerous_command
    if is_dangerous_command(command):
        return PermissionDecision.ASK

    return None


# 软规则：直接返回该工具的默认策略（工具层默认全放行）
def evaluate_soft(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy,
) -> PermissionDecision:
    return policy.default
