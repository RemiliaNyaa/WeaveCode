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

    # 写入文件（已存在即整体覆盖）；参数由 WriteFileParams 统一校验，超 1 MB 拒绝
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        path = Path(p.path)
        if ".." in path.parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")

        encoded = p.content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        path.write_text(p.content, encoding="utf-8")
        return ToolResult(content=f"wrote {len(encoded)} bytes to {path}")
