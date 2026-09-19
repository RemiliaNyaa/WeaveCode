from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 工具参数中可能承载文件系统路径的键名（覆盖内置工具与常见 MCP 工具）
_PATH_PARAM_KEYS: tuple[str, ...] = (
    "path",
    "file_path",
    "filepath",
    "file",
    "directory",
    "dir",
)


# 把可能是相对路径的字符串按工作目录归一化成绝对路径
def normalize_path(raw: str, working_dir: str) -> str:
    p = Path(raw)
    if not p.is_absolute():
        p = Path(working_dir) / p
    return os.path.normpath(str(p))


# 判断路径是否位于工作目录之内（含等于工作目录本身）
def is_within(path: str, working_dir: str) -> bool:
    try:
        target = Path(os.path.normpath(str(path)))
        root = Path(os.path.normpath(str(working_dir)))
    except (ValueError, OSError):
        return False
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


# 返回该路径对应的「批准目录」：目录本身用它自己，文件用它的父目录
def approval_dir(path: str, *, is_dir: bool = False) -> str:
    p = Path(os.path.normpath(str(path)))
    return str(p if is_dir else p.parent)


# 从工具调用参数中提取涉及文件系统的候选路径（绝对化后去重返回）
def extract_paths(tool_name: str, params: dict[str, Any], working_dir: str) -> list[str]:
    # bash：路径藏在命令串里，用 tree-sitter 解析语法树后提取
    if tool_name == "bash":
        command = params.get("command")
        if not isinstance(command, str) or not command:
            return []
        from weavecode.core.permissions.shell_paths import extract_shell_paths
        return extract_shell_paths(command, working_dir)

    raw: list[str] = []
    for key in _PATH_PARAM_KEYS:
        val = params.get(key)
        if isinstance(val, str) and val:
            raw.append(val)
        elif isinstance(val, list):
            raw.extend(str(v) for v in val if isinstance(v, str) and v)
    seen: list[str] = []
    for item in raw:
        norm = normalize_path(item, working_dir)
        if norm not in seen:
            seen.append(norm)
    return seen
