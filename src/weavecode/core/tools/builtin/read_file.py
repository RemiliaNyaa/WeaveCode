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

    # 读整个文件返回纯文本，超过 512 KB 截断；路径按工作目录规范化
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = ReadFileParams.model_validate(params)
        if ".." in Path(p.path).parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")

        path = Path(p.path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()

        data = path.read_bytes()
        truncated = len(data) > _MAX_BYTES
        if truncated:
            data = data[:_MAX_BYTES]
        # CRLF 行尾统一成 \n，避免行尾差异干扰后续比对
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n")
        if truncated:
            text += "\n[truncated]"
        return ToolResult(content=text)
