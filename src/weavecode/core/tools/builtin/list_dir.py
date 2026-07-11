from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_ENTRIES = 200           # 最多展示的条目数
_DEFAULT_DEPTH = 2           # 默认递归深度
_MAX_DEPTH = 4               # 允许的最大递归深度


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    max_depth: int = Field(default=_DEFAULT_DEPTH, ge=1, le=_MAX_DEPTH)


class ListDirTool(BaseTool):
    params_model = ListDirParams
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
        p = ListDirParams.model_validate(params)
        if ".." in Path(p.path).parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")

        root = Path(p.path)
        if not root.exists():
            raise FileNotFoundError(f"directory not found: {p.path}")
        if not root.is_dir():
            raise NotADirectoryError(f"not a directory: {p.path}")

        lines = [f"{p.path}/"]
        _render(root, "", 0, p.max_depth, lines)
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
