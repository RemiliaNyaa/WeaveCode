from __future__ import annotations

from pathlib import Path

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 1 * 1024 * 1024  # 1 MB


class WriteFileTool(BaseTool):
    name = "write_file"
    description = (
        "Write text content to a file. An existing file is replaced entirely.\n"
        "Path must be relative to the current working directory."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the current working directory.",
            },
            "content": {
                "type": "string",
                "description": "Full text content to write.",
            },
        },
        "required": ["path", "content"],
    }

    # 写入文件（已存在即整体覆盖）；内容超 1 MB 拒绝
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        path_str = str(params.get("path", ""))
        content = str(params.get("content", ""))
        if not path_str:
            return ToolResult(content="path and content are required", is_error=True)
        if ".." in Path(path_str).parts:
            raise PermissionError(f"path traversal not allowed: {path_str}")

        encoded = content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
            )

        path = Path(path_str)
        path.write_text(content, encoding="utf-8")
        return ToolResult(content=f"wrote {len(encoded)} bytes to {path}")
