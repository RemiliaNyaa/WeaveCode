from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    content: str


class WriteFileTool(BaseTool):
    params_model = WriteFileParams
    name = "write_file"
    description = (
        "Write text content to a file (create or fully replace).\n"
        "\n"
        "When to use:\n"
        "- Creating a new file.\n"
        "- Replacing the entire content of an existing file.\n"
        "\n"
        "When NOT to use:\n"
        "- Making a small change inside an existing file - use edit_file.\n"
        "- Appending to a file - not supported; read the file, then rewrite the full content.\n"
        "\n"
        "Rules:\n"
        "- Path must be absolute. Parent directories are created automatically.\n"
        "- If the file already exists, its entire content is replaced - this is "
        "destructive. Before overwriting, use list_dir to confirm it exists and "
        "read_file to inspect the current content (skip only if you already know it).\n"
        "- Content size is limited to 1 MB."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file (e.g. 'C:/Users/xxx/file.txt').",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    # 写入文件（不存在则创建并自动建父目录，存在则全量替换）；只支持绝对路径；超 1MB 拒绝
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path_str = p.path
        content = p.content

        path = Path(path_str)
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

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" 关掉文本模式的行尾翻译：内容里是 \n 就写 \n，是 \r\n 就写 \r\n
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(content)
        return ToolResult(content=f"wrote {len(encoded)} bytes to {path}")
