from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_ENTRIES = 200           # 最多展示的条目数
_DEFAULT_DEPTH = 2           # 默认递归深度
_MAX_DEPTH = 4               # 允许的最大递归深度


class ListDirTool(BaseTool):
    name = "list_dir"
    description = (
        "List a directory tree recursively.\n"
        "Path must be relative to the current working directory.\n"
        f"Default depth is {_DEFAULT_DEPTH} (max {_MAX_DEPTH})."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to the current working directory.",
            },
            "max_depth": {
                "type": "integer",
                "description": (
                    f"How deep to recurse (default {_DEFAULT_DEPTH}, max {_MAX_DEPTH})."
                ),
            },
        },
        "required": ["path"],
    }

    # 树状递归展示目录：层级前缀 + 目录斜杠，默认深度 2，按 200 条截断
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = str(params.get("path", ""))
        if not path_str:
            return ToolResult(content="path is required", is_error=True)
        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        try:
            max_depth = int(params.get("max_depth", _DEFAULT_DEPTH))
        except (TypeError, ValueError):
            max_depth = _DEFAULT_DEPTH
        max_depth = max(1, min(_MAX_DEPTH, max_depth))

        root = Path(path_str)
        if not root.exists():
            raise FileNotFoundError(f"directory not found: {path_str}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {path_str}")

        lines = [f"{path_str}/"]
        _render(root, "", 0, max_depth, lines)
        return ToolResult(content="\n".join(lines))


# 递归渲染目录树；条目数触顶时追加截断提示并停止
def _render(directory: Path, prefix: str, depth: int, max_depth: int, lines: list[str]) -> None:
    entries = sorted(directory.iterdir(), key=lambda e: e.name)
    for index, entry in enumerate(entries):
        if len(lines) >= _MAX_ENTRIES:
            lines.append("...(truncated)")
            return
        last = index == len(entries) - 1
        connector = "└── " if last else "├── "
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{prefix}{connector}{entry.name}{suffix}")
        if entry.is_dir() and depth + 1 < max_depth:
            child_prefix = prefix + ("    " if last else "│   ")
            _render(entry, child_prefix, depth + 1, max_depth, lines)
