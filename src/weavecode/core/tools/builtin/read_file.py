from __future__ import annotations

from difflib import get_close_matches
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from weavecode.core.tools.base import BaseTool, ToolResult

_DEFAULT_LIMIT = 2000          # 每次最多读取的行数
_MAX_BYTES = 50 * 1024         # 每次最多返回 50 KB 文本
_MAX_BYTES_LABEL = "50 KB"
_BINARY_EXTENSIONS = frozenset({
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar", ".war",
    ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib", ".wasm", ".pyc",
    ".pyo", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".bmp", ".ico",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".flac",
})
_SUGGEST_MAX = 3
_SUGGEST_CUTOFF = 0.5


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    offset: int = Field(default=1, ge=1)      # 从第几行开始读（1-indexed）
    limit: int = Field(default=_DEFAULT_LIMIT, ge=1)


# 检查文件是否为二进制：扩展名命中或内容采样含 NUL/高比例不可打印字符
def _is_binary(path: Path, sample: bytes) -> bool:
    if path.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    non_printable = sum(
        1 for b in sample if b == 0 or (b < 9 or (b > 13 and b < 32))
    )
    return non_printable / len(sample) > 0.3


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
    name = "read_file"
    description = (
        "Read the text content of a file, with line numbers.\n"
        "Path must be absolute (e.g. 'C:/Users/xxx/file.txt').\n"
        "Reads up to 2000 lines / 50 KB per call; use offset to page through a "
        "large file."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file (e.g. 'C:/Users/xxx/file.txt').",
            },
            "offset": {
                "type": "integer",
                "description": "The line number to start reading from (1-indexed, default 1).",
            },
            "limit": {
                "type": "integer",
                "description": f"Maximum number of lines to read (default {_DEFAULT_LIMIT}).",
            },
        },
        "required": ["path"],
    }

    # 读取文件指定行区间（只支持绝对路径）；每行带行号；超限截断提示；非文本返回不支持提示
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ReadFileParams.model_validate(params)
        path = Path(p.path)

        if not path.is_absolute():
            return ToolResult(
                content=(
                    f"Invalid path: {path}\n"
                    "This tool requires an absolute path "
                    "(e.g. 'C:/Users/xxx/file.txt' or '/home/xxx/file.txt')."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        return _read_sync(path, p.offset, p.limit)


# 读文件并渲染成带行号的文本
def _read_sync(path: Path, offset: int, limit: int) -> ToolResult:
    if not path.exists():
        raise FileNotFoundError(_missing_message(path))
    if path.is_dir():
        raise IsADirectoryError(f"cannot read a directory: {path}")

    sample = path.read_bytes()[:_MAX_BYTES + 4096]
    if _is_binary(path, sample):
        return ToolResult(
            content=(
                f"Cannot read file: {path}\n"
                "Unsupported file type: this tool only reads text files "
                "(binary, image and PDF files are not supported)."
            ),
            is_error=True,
            error_type="runtime_error",
        )

    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # offset 越界仅在文件有内容时才算错误；空文件直接返回空内容
    if len(lines) > 0 and offset > len(lines):
        return ToolResult(
            content=f"Offset {offset} is out of range for this file ({len(lines)} lines).",
            is_error=True,
            error_type="runtime_error",
        )

    # 逐行累积，同时受行数和字节上限约束；返回 (行号, 内容) 列表
    numbered: list[tuple[int, str]] = []
    bytes_used = 0
    cut = False
    for idx in range(offset - 1, len(lines)):
        if len(numbered) >= limit:
            break
        line = lines[idx]
        size = len(line.encode("utf-8")) + 1
        if bytes_used + size > _MAX_BYTES:
            cut = True
            break
        numbered.append((idx + 1, line))
        bytes_used += size

    total = len(lines)
    if total == 0:
        return ToolResult(content="")

    last = numbered[-1][0] if numbered else offset - 1
    next_offset = last + 1

    output_parts = [f"{num}: {line}" for num, line in numbered]
    footer = _footer(offset, last, total, cut, next_offset, len(numbered) < limit and not cut)
    return ToolResult(content="\n".join(output_parts) + footer)


# 生成文件读取结果末尾的区间/总数/继续提示
def _footer(
    start: int, last: int, total: int, cut: bool, next_offset: int, ended: bool
) -> str:
    if cut:
        return (
            f"\n\n(Output capped at {_MAX_BYTES_LABEL}. "
            f"Showing lines {start}-{last}. Use offset={next_offset} to continue.)"
        )
    if not ended:
        return (
            f"\n\n(Showing lines {start}-{last} of {total}. "
            f"Use offset={next_offset} to continue.)"
        )
    return f"\n\n(End of file - total {total} lines)"


# 生成"文件未找到"的错误消息；同目录下有相似文件名时附带建议
def _missing_message(path: Path) -> str:
    msg = f"File not found: {path}"
    directory = path.parent
    if not directory.is_dir():
        return msg
    try:
        candidates = [
            p.name for p in directory.iterdir() if p.is_file()
        ]
    except OSError:
        return msg
    suggestions = get_close_matches(path.name, candidates, n=_SUGGEST_MAX, cutoff=_SUGGEST_CUTOFF)
    if suggestions:
        msg += "\n\nDid you mean one of these?\n" + "\n".join(
            str(directory / name) for name in suggestions
        )
    return msg
