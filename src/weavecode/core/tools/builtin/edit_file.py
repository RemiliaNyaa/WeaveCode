from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from weavecode.core.tools.base import BaseTool, ToolResult


class EditFileParams(BaseModel):
    model_config = ConfigDict(extra="ignore")
    path: str
    old_string: str
    new_string: str


class EditFileTool(BaseTool):
    params_model = EditFileParams
    name = "edit_file"
    description = (
        "Replace an exact substring in a file.\n"
        "Path must be absolute.\n"
        "old_string must match the file content exactly, including spaces, "
        "indentation, and line endings.\n"
        "If old_string matches multiple locations, only the first is replaced and "
        "the result reports the total count.\n"
        "This tool only modifies existing files."
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

        with path.open("r", encoding="utf-8", newline="") as f:
            content = f.read()

        ending = "\r\n" if "\r\n" in content else "\n"
        old = _convert_to_line_ending(_normalize_line_endings(old_string), ending)
        new = _convert_to_line_ending(_normalize_line_endings(new_string), ending)

        if old == new:
            return ToolResult(content="Old and new strings are identical; no change was made.")

        count = content.count(old)
        if count == 0:
            return ToolResult(
                content=(
                    "Could not find old_string in the file. "
                    "It must match exactly, including spaces, indentation, and line endings."
                ),
                is_error=True,
                error_type="runtime_error",
            )

        new_content = content.replace(old, new, 1)
        with path.open("w", encoding="utf-8", newline="") as f:
            f.write(new_content)

        msg = f"Edit applied successfully to {path}."
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
