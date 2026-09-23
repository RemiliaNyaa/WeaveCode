from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult
from weavecode.core.tools.file_mutation import (
    DEFAULT_FILE_MUTATION,
    EditRejectedError,
    FileMutation,
)


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_string: str
    new_string: str


class EditFileTool(BaseTool):
    params_model = EditFileParams
    name = "edit_file"
    description = (
        "Replace an exact substring in an existing file.\n"
        "\n"
        "When to use:\n"
        "- Making a targeted change to a specific part of an existing file.\n"
        "- Fixing a small bug, renaming, or updating one line/block.\n"
        "\n"
        "When NOT to use:\n"
        "- Creating a new file or replacing the whole file - use write_file.\n"
        "- You do not know the current content - read the file first.\n"
        "\n"
        "Rules:\n"
        "- Path must be absolute.\n"
        "- old_string must match the file content exactly, including spaces, "
        "indentation, and line endings.\n"
        "- If old_string matches multiple locations, only the first is replaced and the "
        "result reports the total count - pass an old_string that matches uniquely "
        "(add surrounding context if needed).\n"
        "- This tool only modifies existing files."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file (e.g. 'C:/Users/xxx/file.txt').",
            },
            "old_string": {
                "type": "string",
                "description": (
                    "Exact text to replace. Must match the file content precisely, "
                    "including spaces, indentation, and line endings."
                ),
            },
            "new_string": {
                "type": "string",
                "description": "Text to replace it with.",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }

    # 注入写入服务（默认用进程级单例；测试可传自己的实例以隔离锁表）
    def __init__(self, mutation: FileMutation | None = None) -> None:
        self._mutation = mutation or DEFAULT_FILE_MUTATION

    # 编辑已有文件（精确替换第一处）；只支持绝对路径；空串/新旧相同/不存在分报错；行尾归一化
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        p = EditFileParams.model_validate(params)
        path = Path(p.path)
        old_string = p.old_string
        new_string = p.new_string

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

        if not path.exists():
            return ToolResult(
                content=(
                    f"File not found: {path}\n"
                    "This tool only edits existing files. "
                    "Use write_file to create a new file or replace an entire file."
                ),
                is_error=True,
                error_type="runtime_error",
            )
        if path.is_dir():
            return ToolResult(
                content=f"Cannot edit a directory: {path}",
                is_error=True,
                error_type="runtime_error",
            )

        if old_string == "":
            return ToolResult(
                content=(
                    "old_string cannot be empty. "
                    "Provide the exact text to replace, or use write_file for an "
                    "intentional full-file replacement."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        # 行尾归一化对 old / new 是同一套变换，原始串相同就必然相同 → 提前判定，不必读文件
        if old_string == new_string:
            return ToolResult(content="Old and new strings are identical; no change was made.")

        box: dict[str, int] = {}

        # 在文件锁内、线程里跑「读 → 改 → 写」；找不到 old_string 就抛错中止（绝不写）
        def transform(content: str) -> str:
            ending = "\r\n" if "\r\n" in content else "\n"
            old = _convert_to_line_ending(_normalize_line_endings(old_string), ending)
            new = _convert_to_line_ending(_normalize_line_endings(new_string), ending)
            count = content.count(old)
            if count == 0:
                raise EditRejectedError(
                    "Could not find old_string in the file. "
                    "It must match exactly, including spaces, indentation, and line endings."
                )
            box["count"] = count
            return content.replace(old, new, 1)

        try:
            await self._mutation.edit(path, transform)
        except EditRejectedError as exc:
            return ToolResult(content=exc.message, is_error=True, error_type="runtime_error")
        except OSError as exc:
            return ToolResult(
                content=f"Failed to edit {path}: {exc}",
                is_error=True,
                error_type="runtime_error",
            )

        msg = f"Edit applied successfully to {path}."
        count = box.get("count", 1)
        if count > 1:
            msg += f" Note: old_string matched {count} locations; replaced the first one."
        return ToolResult(content=msg)


# 统一换行符为 \n，便于跨平台比较
def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n")


# 按目标行尾样式转换文本（\n → \r\n），保持与原文件一致
def _convert_to_line_ending(text: str, ending: str) -> str:
    if ending == "\n":
        return text
    return text.replace("\n", "\r\n")