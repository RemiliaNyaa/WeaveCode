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

    # 写入文件（已存在即整体覆盖）；路径按工作目录规范化，行尾按内容原样落盘
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = WriteFileParams.model_validate(params)
        if ".." in Path(p.path).parts:
            raise PermissionError(f"path traversal not allowed: {p.path}")

        path = Path(p.path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()

        encoded = p.content.encode("utf-8")
        if len(encoded) > _MAX_BYTES:
            return ToolResult(
                content=f"content too large: {len(encoded)} bytes (limit 1 MB)",
                is_error=True,
                error_type="runtime_error",
            )

        # newline="" 关掉文本模式的行尾翻译：内容里是 \n 就写 \n，是 \r\n 就写 \r\n
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(p.content)
        return ToolResult(content=f"wrote {len(encoded)} bytes to {path}")
