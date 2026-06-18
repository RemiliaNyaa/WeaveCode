from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileTool(BaseTool):
    name = "read_file"
    description = (
        "Read the content of a file.\n"
        "Path must be relative to the current working directory."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the current working directory.",
            },
        },
        "required": ["path"],
    }

    # 读整个文件返回纯文本，超过 512 KB 截断
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = str(params.get("path", ""))
        if not path_str:
            return ToolResult(content="path is required", is_error=True)
        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        data = Path(path_str).read_bytes()
        if len(data) > _MAX_BYTES:
            data = data[:_MAX_BYTES]
            return ToolResult(
                content=data.decode("utf-8", errors="replace") + "\n[truncated]"
            )
        return ToolResult(content=data.decode("utf-8", errors="replace"))
