from __future__ import annotations

import re
from dataclasses import dataclass, field
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


@dataclass
class ToolPolicy:
    # 默认决策：没有命中任何名单时按它处理
    default: PermissionDecision
    # 命中即拒绝的命令正则（硬规则）
    deny_patterns: list[str] = field(default_factory=list)
    # 命中即放行的命令正则（软规则，可被用户的选择覆盖）
    allow_patterns: list[str] = field(default_factory=list)


# 工具默认策略：按「是否影响外部世界」划分，只有会改写状态的操作默认询问
DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":        ToolPolicy(default=PermissionDecision.ASK),   # 执行命令，影响外部
    "write_file":  ToolPolicy(default=PermissionDecision.ASK),   # 写文件，影响外部
    "edit_file":   ToolPolicy(default=PermissionDecision.ASK),   # 改文件，影响外部
    "read_file":   ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":    ToolPolicy(default=PermissionDecision.ALLOW),
    "glob":        ToolPolicy(default=PermissionDecision.ALLOW),
    "grep":        ToolPolicy(default=PermissionDecision.ALLOW),
    "update_plan": ToolPolicy(default=PermissionDecision.ALLOW),
    "spawn_agent": ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":   ToolPolicy(default=PermissionDecision.ALLOW),
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
    "note_save":  "content",
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


# 硬规则：黑名单，不可被任何缓存绕过；未命中返回 None
# 路径越界不再在这里判断，统一由 paths/shell_paths 提取候选路径后按 external_directory 处理
def evaluate_hard(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy,
) -> PermissionDecision | None:
    command = str(params.get("command", "")) if tool_name == "bash" else ""
    if not command:
        return None  # 非 bash 工具没有 command → 无硬规则

    for pat in policy.deny_patterns:       # 黑名单：命中直接拒绝
        if re.search(pat, command):
            return PermissionDecision.DENY

    return None


# 软规则：白名单 + 默认策略，可被缓存覆盖；返回 ALLOW/DENY/ASK
def evaluate_soft(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy,
) -> PermissionDecision:
    command = str(params.get("command", "")) if tool_name == "bash" else ""
    if command:
        for pat in policy.allow_patterns:   # 白名单：命中直接放行
            if re.search(pat, command):
                return PermissionDecision.ALLOW
    return policy.default
