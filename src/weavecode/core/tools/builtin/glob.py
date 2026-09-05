from __future__ import annotations

import os
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult

_LIMIT = 100                      # 最多返回的匹配文件数
# 遍历时跳过常见的工具/依赖/缓存目录，避免搜索爆炸（对齐 rg 默认忽略行为）
_IGNORED_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", ".tox", ".nox",
    "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "__pycache__", "dist", "build",
})


class GlobParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str                 # glob 文件名模式（如 **/*.py）
    path: str                    # 必填：绝对路径目录


class GlobTool(BaseTool):
    params_model = GlobParams
    name = "glob"
    description = (
        "Find files by name patterns.\n"
        "\n"
        "When to use:\n"
        "- You need to find files by name pattern (e.g. '**/*.py', 'src/**/*.ts').\n"
        "- You need a quick list of matching file paths before reading them.\n"
        "\n"
        "When NOT to use:\n"
        "- You need to search file contents - use grep.\n"
        "- You need the content of a known file - use read_file.\n"
        "\n"
        "Rules:\n"
        "- Path must be an absolute directory path; the pattern matches file names "
        "recursively below it.\n"
        "- Common tool/vendor directories (.git, node_modules, __pycache__, etc.) "
        "are skipped.\n"
        f"- Returns up to {_LIMIT} matching file paths; larger results are truncated."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern to match file names against "
                               "(e.g. '**/*.py', 'src/**/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to the directory to search "
                               "(e.g. 'C:/Users/xxx/project').",
            },
        },
        "required": ["pattern", "path"],
    }

    # 按文件名 glob 递归匹配（只支持绝对路径目录）；跳过忽略目录；返回绝对路径列表
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GlobParams.model_validate(params)
        root = Path(p.path)

        if not root.is_absolute():
            return ToolResult(
                content=(
                    f"Invalid path: {root}\n"
                    "This tool requires an absolute path "
                    "(e.g. 'C:/Users/xxx/project' or '/home/xxx/project')."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        return _glob_sync(root, p.pattern)


# 递归匹配文件名
def _glob_sync(root: Path, pattern: str) -> ToolResult:
    if not root.exists():
        return ToolResult(
            content=f"Directory not found: {root}",
            is_error=True,
            error_type="runtime_error",
        )
    if not root.is_dir():
        return ToolResult(
            content=f"Not a directory: {root}\nThis path is a file.",
            is_error=True,
            error_type="runtime_error",
        )

    matches: list[str] = []
    for f in _walk_files(root):
        rel = f.relative_to(root)
        if _rel_matches(rel, pattern):
            matches.append(str(f))
            if len(matches) >= _LIMIT:
                break

    if not matches:
        return ToolResult(content="No files found")

    lines = list(matches)
    if len(matches) == _LIMIT:
        lines.append("")
        lines.append(
            f"(Results are truncated: showing first {_LIMIT} results. "
            "Consider using a more specific path or pattern.)"
        )
    return ToolResult(content="\n".join(lines))


# 递归生成 root 下所有文件（跳过忽略目录），用于 glob/grep 的公共遍历
def _walk_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORED_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


# 判断相对路径是否匹配 glob 模式（支持 ** 任意深度含零层、无目录部分时匹配任意层级 basename）
def _rel_matches(rel: Path, pattern: str) -> bool:
    return _glob_to_regex(pattern).fullmatch(rel.as_posix()) is not None


# 把 glob 模式转成正则：** 任意深度目录（含零层）、* 单段、? 单字符、[...] 字符类
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                out.append(r"(?:.*/)?")
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1  # **/ 整体表示"任意深度目录"，吞掉紧跟的斜杠
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                cls = pattern[i + 1 : j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append(f"[{cls}]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    body = "".join(out)
    if "/" not in pattern:
        body = r"(?:.*/)?" + body
    return re.compile("^" + body + "$")
