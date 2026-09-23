from __future__ import annotations

import asyncio
import fnmatch
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.builtin.glob import _walk_files

_LIMIT = 100                      # 最多返回的匹配条数
_BINARY_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar", ".war",
    ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib", ".wasm", ".pyc",
    ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".bmp", ".ico",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
})


class GrepParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern: str                 # 正则表达式（搜文件内容）
    path: str                    # 必填：绝对路径（目录递归 / 单个文件）
    include: str = ""            # 可选：文件名模式过滤（如 *.py、*.{ts,tsx}）


class GrepTool(BaseTool):
    params_model = GrepParams
    name = "grep"
    description = (
        "Search file contents with a regular expression.\n"
        "\n"
        "When to use:\n"
        "- You need to find which files/lines contain a pattern "
        "(e.g. 'log.*Error', 'function\\\\s+\\\\w+').\n"
        "- You need to locate a symbol, error string, or keyword in the codebase.\n"
        "\n"
        "When NOT to use:\n"
        "- You only need file names by pattern - use glob.\n"
        "- You need to count the number of matches - keep it in this tool; "
        "each result carries its line number.\n"
        "\n"
        "Rules:\n"
        "- Path must be an absolute directory (searched recursively) or a single "
        "file path.\n"
        f"- Returns up to {_LIMIT} matches grouped by file; larger results are "
        "truncated.\n"
        "- Binary files and common tool/vendor directories are skipped."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regular expression to search for in file contents.",
            },
            "path": {
                "type": "string",
                "description": "Absolute path to the directory or file to search "
                               "(e.g. 'C:/Users/xxx/project').",
            },
            "include": {
                "type": "string",
                "description": "Optional file name pattern to filter results "
                               "(e.g. '*.py', '*.{ts,tsx}').",
            },
        },
        "required": ["pattern", "path"],
    }

    # 按正则搜索文件内容（只支持绝对路径）；跳二进制/忽略目录；方案A分组输出
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = GrepParams.model_validate(params)
        path = Path(p.path)

        if not path.is_absolute():
            return ToolResult(
                content=(
                    f"Invalid path: {path}\n"
                    "This tool requires an absolute path "
                    "(e.g. 'C:/Users/xxx/project' or '/home/xxx/project')."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        try:
            rx = re.compile(p.pattern)
        except re.error as exc:
            return ToolResult(
                content=f"Invalid regex pattern: {p.pattern}\n{exc}",
                is_error=True,
                error_type="runtime_error",
            )

        # 磁盘 IO 放进线程：整棵目录树逐个读文件，是最重的一个工具，绝不能让事件循环陪着卡住
        return await asyncio.to_thread(_grep_sync, path, rx, p.include)


# 同步核心：遍历 + 逐文件匹配（在线程里执行，整段不让出）
def _grep_sync(path: Path, rx: re.Pattern[str], include: str) -> ToolResult:
    if not path.exists():
        return ToolResult(
            content=f"Path not found: {path}",
            is_error=True,
            error_type="runtime_error",
        )

    rows: list[tuple[str, int, str]] = []
    if path.is_dir():
        for f in _walk_files(path):
            _search_file(f, rx, include, rows, _LIMIT)
            if len(rows) >= _LIMIT:
                break
    else:
        _search_file(path, rx, include, rows, _LIMIT)

    if not rows:
        return ToolResult(content="No files found")
    return ToolResult(content=_format_rows(rows))


# 搜索单个文件：include 过滤、二进制跳过、逐行正则匹配，结果追加到 rows
def _search_file(
    path: Path,
    rx: re.Pattern[str],
    include: str,
    rows: list[tuple[str, int, str]],
    limit: int,
) -> None:
    if include and not _matches_include(path.name, include):
        return
    if _is_binary(path):
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        if rx.search(line):
            rows.append((str(path), lineno, line))
            if len(rows) >= limit:
                return


# 判断 include 是否命中文件名（支持 {a,b} 花括号展开后逐个 fnmatch）
def _matches_include(name: str, include: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in _expand_braces(include))


# 展开 glob 的花括号模式（如 *.{ts,tsx} → ['*.ts', '*.tsx']）；无花括号原样返回
def _expand_braces(pattern: str) -> list[str]:
    m = re.search(r"\{([^}]+)\}", pattern)
    if not m:
        return [pattern]
    options = m.group(1).split(",")
    return [pattern[: m.start()] + opt + pattern[m.end() :] for opt in options]


# 判断文件是否为二进制：扩展名命中或内容采样含 NUL/高比例不可打印字符
def _is_binary(path: Path) -> bool:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as fh:
            sample = fh.read(8192)
    except OSError:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    non_printable = sum(1 for b in sample if b < 9 or (b > 13 and b < 32))
    return non_printable / len(sample) > 0.3


# 按文件分组输出匹配（方案A）；满 _LIMIT 时追加截断提示
def _format_rows(rows: list[tuple[str, int, str]]) -> str:
    suffix = " (more matches available)" if len(rows) == _LIMIT else ""
    lines = [f"Found {len(rows)} matches{suffix}"]
    current = ""
    for path, lineno, text in rows:
        if current != path:
            if current:
                lines.append("")
            current = path
            lines.append(f"{path}:")
        lines.append(f"  Line {lineno}: {text}")
    if len(rows) == _LIMIT:
        lines.append("")
        lines.append("(Results truncated. Consider using a more specific path or pattern.)")
    return "\n".join(lines)