from __future__ import annotations

import math
from difflib import get_close_matches
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from weavecode.core.tools.base import BaseTool, ToolResult

_PAGE_SIZE = 100              # 每页最多显示的条目数
_SUGGEST_MAX = 3
_SUGGEST_CUTOFF = 0.5


class ListDirParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    page: int = Field(default=1, ge=1)    # 页码（1-indexed）


class ListDirTool(BaseTool):
    params_model = ListDirParams
    name = "list_dir"
    description = (
        "List the direct children of a directory in a flat list.\n"
        "\n"
        "When to use:\n"
        "- You need to discover what files/directories exist under a path.\n"
        "- You need to verify whether a file exists before writing over it.\n"
        "\n"
        "When NOT to use:\n"
        "- You need the content of a file - use read_file.\n"
        "- You need to traverse a whole tree recursively - list the relevant "
        "directory and drill into subdirectories by name.\n"
        "\n"
        "Rules:\n"
        "- Path must be an absolute path to a directory.\n"
        "- Entries are shown one per line, sorted by name; directories are suffixed with '/'.\n"
        "- Subdirectories are NOT recursed.\n"
        f"- At most {_PAGE_SIZE} entries per page; pass page=N to read later pages."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the directory (e.g. 'C:/Users/xxx/project').",
            },
            "page": {
                "type": "integer",
                "description": (
                    f"Page number to view (default 1, "
                    f"at most {_PAGE_SIZE} entries per page)."
                ),
            },
        },
        "required": ["path"],
    }

    # 平铺列出目录直接子项（每页最多 _PAGE_SIZE 条）；只支持绝对路径且必须是目录；不递归
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ListDirParams.model_validate(params)
        path = Path(p.path)

        if not path.is_absolute():
            return ToolResult(
                content=(
                    f"Invalid path: {path}\n"
                    "This tool requires an absolute path "
                    "(e.g. 'C:/Users/xxx' or '/home/xxx')."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        return _list_sync(path, p.page)


# 列目录并分页渲染
def _list_sync(path: Path, page: int) -> ToolResult:
    if not path.exists():
        return ToolResult(
            content=_missing_dir_message(path),
            is_error=True,
            error_type="runtime_error",
        )
    if not path.is_dir():
        return ToolResult(
            content=(
                f"Not a directory: {path}\n"
                "This path is a file. Use read_file to read its content."
            ),
            is_error=True,
            error_type="runtime_error",
        )

    entries = sorted(path.iterdir(), key=lambda e: e.name)
    total = len(entries)
    total_pages = max(1, math.ceil(total / _PAGE_SIZE))

    if page > total_pages:
        return ToolResult(
            content=(
                f"Page {page} is out of range. "
                f"Total {total} entries, {total_pages} page(s)."
            ),
            is_error=True,
            error_type="runtime_error",
        )

    start = (page - 1) * _PAGE_SIZE
    end = min(start + _PAGE_SIZE, total)
    sliced = entries[start:end]

    lines = [
        f"<path>{path}</path>",
        "<type>directory</type>",
        "<entries>",
    ]
    for entry in sliced:
        suffix = "/" if entry.is_dir() else ""
        lines.append(f"{entry.name}{suffix}")
    lines.append("</entries>")

    if total == 0:
        footer = "(0 entries, page 1 of 1.)"
    elif end < total:
        footer = (
            f"(Showing entries {start + 1}-{end} of {total}. "
            f"Page {page} of {total_pages}. Use page={page + 1} to see more.)"
        )
    else:
        footer = (
            f"(Showing entries {start + 1}-{end} of {total}. "
            f"Page {page} of {total_pages}.)"
        )

    return ToolResult(content="\n".join(lines) + "\n\n" + footer)


# 生成"目录未找到"的错误消息；同目录下有相似目录名时附带建议（只列目录）
def _missing_dir_message(path: Path) -> str:
    msg = f"Directory not found: {path}"
    directory = path.parent
    if not directory.is_dir():
        return msg
    try:
        candidates = [p.name for p in directory.iterdir() if p.is_dir()]
    except OSError:
        return msg
    suggestions = get_close_matches(path.name, candidates, n=_SUGGEST_MAX, cutoff=_SUGGEST_CUTOFF)
    if suggestions:
        msg += "\n\nDid you mean one of these?\n" + "\n".join(
            f"{directory / name}/" for name in suggestions
        )
    return msg
