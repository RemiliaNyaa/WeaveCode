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
class ToolPolicy:
    # 默认决策：没有命中任何名单时按它处理
    default: PermissionDecision
    # 命中即拒绝的命令正则（硬规则）
    deny_patterns: list[str] = field(default_factory=list)
    # 命中即放行的命令正则（软规则，可被用户的选择覆盖）
    allow_patterns: list[str] = field(default_factory=list)


# 越界启发式：bash 命令出现这些形态即视为可能访问了工作目录之外，强制询问
OUTSIDE_CWD_HEURISTICS: list[str] = [
    r"(^|\s)/[^\s]",
    r"(^|\s)~",
    r"(^|\s)\.\.(/|$|\s)",
    r"\$\{?HOME\b",
    r"\$\{?PWD\b",
    r"(^|\s|;|&&|\|\|)cd(\s|$)",
]


# 命令是否命中越界启发式
def matches_outside_cwd(command: str) -> bool:
    return any(re.search(pat, command) for pat in OUTSIDE_CWD_HEURISTICS)


# 工具默认策略：按「是否影响外部世界」划分，Agent 内部的事默认放行
DEFAULT_POLICIES: dict[str, ToolPolicy] = {
    "bash":       ToolPolicy(default=PermissionDecision.ASK),   # 执行命令，影响外部
    "write_file": ToolPolicy(default=PermissionDecision.ASK),   # 写文件，影响外部
    "read_file":  ToolPolicy(default=PermissionDecision.ALLOW),
    "list_dir":   ToolPolicy(default=PermissionDecision.ALLOW),
    "note_save":  ToolPolicy(default=PermissionDecision.ALLOW),
    # 任务管理类：Agent 自己拆解、推进任务的内部操作
    "task_list":    ToolPolicy(default=PermissionDecision.ALLOW),
    "task_get":     ToolPolicy(default=PermissionDecision.ALLOW),
    "task_create":  ToolPolicy(default=PermissionDecision.ALLOW),
    "task_update":  ToolPolicy(default=PermissionDecision.ALLOW),
    # 查询子 Agent 结果与派生子 Agent 本身没有副作用
    "agent_result": ToolPolicy(default=PermissionDecision.ALLOW),
    "spawn_agent":  ToolPolicy(default=PermissionDecision.ALLOW),
}

# 未在 DEFAULT_POLICIES 中登记的工具（如 MCP 工具）的兜底策略：保守询问
_UNKNOWN_TOOL_DEFAULT = PermissionDecision.ASK

# bash 参数中展示用的关键字段映射
_PREVIEW_KEY: dict[str, str] = {
    "bash":       "command",
    "read_file":  "path",
    "write_file": "path",
    "list_dir":   "path",
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


# 静态判定：黑名单 → 越界 → 白名单 → 默认策略
def evaluate(
    tool_name: str,
    params: dict[str, Any],
    policy: ToolPolicy | None = None,
) -> PermissionDecision:
    if policy is None:
        policy = DEFAULT_POLICIES.get(tool_name)
    if policy is None:
        return _UNKNOWN_TOOL_DEFAULT

    command = str(params.get("command", "")) if tool_name == "bash" else ""

    if command:
        for pat in policy.deny_patterns:
            if re.search(pat, command):
                return PermissionDecision.DENY

    if command and matches_outside_cwd(command):
        return PermissionDecision.ASK

    if command:
        for pat in policy.allow_patterns:
            if re.search(pat, command):
                return PermissionDecision.ALLOW

    return policy.default
