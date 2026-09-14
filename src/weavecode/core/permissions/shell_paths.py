from __future__ import annotations

from pathlib import Path

from weavecode.core.permissions.paths import normalize_path
from weavecode.core.permissions.shell_parse import (
    iter_command_nodes,
    node_text,
    parse,
    unquote,
)

# 「参数是文件/目录路径」的命令白名单（对齐 opencode 的 FILES/CMD_FILES，并补齐读类命令）
_PATH_COMMANDS: frozenset[str] = frozenset({
    # 目录切换
    "cd", "pushd", "popd",
    # 读取类（本项目的重点：避免模型读到工作目录外的内容）
    "cat", "head", "tail", "less", "more", "bat", "nl", "tac",
    "ls", "tree", "find", "fd", "du", "stat", "file", "wc", "diff", "cmp",
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    # 写入/移动类
    "rm", "cp", "mv", "mkdir", "touch", "chmod", "chown", "ln", "rmdir",
})


# 解析 bash 命令，提取其中涉及文件系统的候选路径（绝对化后去重）
def extract_shell_paths(command: str, working_dir: str) -> list[str]:
    root = parse(command)
    if root is None:
        return []

    out: list[str] = []
    for cmd_node in iter_command_nodes(root):
        if not cmd_node.children:
            continue
        name = node_text(cmd_node.children[0]).lower()
        name = name.rsplit("/", 1)[-1]
        if name not in _PATH_COMMANDS:
            continue
        for arg_node in cmd_node.children[1:]:
            raw = node_text(arg_node)
            if not raw or raw.startswith("-"):
                continue
            resolved = _resolve_arg(raw, working_dir)
            if resolved and resolved not in out:
                out.append(resolved)
    return out


# 把命令里的一个参数解析成绝对路径；无法安全解析时返回 None
def _resolve_arg(raw: str, working_dir: str) -> str | None:
    text = unquote(raw)
    # 展开 ~ 为家目录
    if text == "~" or text.startswith("~/"):
        text = str(Path.home()) + text[1:]
    # 动态内容（$VAR、$(cmd)、反引号）无法静态解析 → 放弃，避免误判
    if "$" in text or "`" in text or text.startswith("("):
        return None
    # 通配符：截断到通配符之前的前缀（如 /data/*.txt → /data/）
    for ch in ("*", "?", "["):
        idx = text.find(ch)
        if idx == 0:
            return None
        if idx > 0:
            text = text[:idx]
            break
    if not text:
        return None
    return normalize_path(text, working_dir)
