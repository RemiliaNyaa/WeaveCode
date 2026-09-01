from __future__ import annotations

from typing import Any

from weavecode.core.mcp.client import McpSessionHandle
from weavecode.core.tools.base import BaseTool, ToolResult


# 将 MCP 工具包装为 BaseTool，使 ToolRegistry 可透明调用（与内置工具同接口）
class McpTool(BaseTool):
    params_model = None  # input_schema 来自 MCP tool_def，不使用 pydantic model

    # 初始化 MCP 工具包装器，工具名以 server_name__ 为前缀防止命名冲突
    def __init__(self, handle: McpSessionHandle, server_name: str, tool: Any) -> None:
        self._handle = handle
        self._server_name = server_name
        self._tool = tool
        self.name = f"{server_name}__{tool.name}"
        self.description = tool.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = (
            tool.input_schema or {"type": "object", "properties": {}}
        )

    # 调用 MCP server 上的工具；连接不可用或执行失败时返回 is_error=True
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            result = await self._handle.call_tool(self._tool.name, dict(params))
            if getattr(result, "is_error", False):
                return ToolResult(
                    content=_content_text(result) or f"mcp tool '{self.name}' failed",
                    is_error=True,
                    error_type="runtime_error",
                )
            return ToolResult(content=_content_text(result) or "")
        except Exception as exc:
            return ToolResult(
                content=f"mcp tool '{self.name}' error: {exc}",
                is_error=True,
                error_type="runtime_error",
            )


# 从官方 SDK 的 CallToolResult 中提取所有 text 类型内容，拼接为字符串
def _content_text(result: Any) -> str:
    parts: list[str] = []
    for item in getattr(result, "content", []) or []:
        if getattr(item, "type", "") == "text":
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
    return "\n".join(parts)