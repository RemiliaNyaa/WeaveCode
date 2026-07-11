from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_BYTES = 512 * 1024  # 512 KB


class ReadFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str


class ReadFileTool(BaseTool):
    params_model = ReadFileParams
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

    # 读整个文件返回纯文本，超过 512 KB 截断；参数由 ReadFileParams 统一校验
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ReadFileParams.model_validate(params)
        path = Path(p.path)
        if ".." in path.parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")

        data = path.read_bytes()
        if len(data) > _MAX_BYTES:
            data = data[:_MAX_BYTES]
            return ToolResult(
                content=data.decode("utf-8", errors="replace") + "\n[truncated]"
            )
        return ToolResult(content=data.decode("utf-8", errors="replace"))
