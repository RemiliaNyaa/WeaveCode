from __future__ import annotations

from typing import Any

from weavecode.core.mcp.client import McpClient, McpServerUnavailableError, McpToolDef
from weavecode.core.tools.base import BaseTool, ToolResult


# 将 MCP 工具包装为 BaseTool，使 ToolRegistry 可透明调用（与内置工具同接口）
class McpTool(BaseTool):
    params_model = None  # input_schema 来自 MCP tool_def，不使用 pydantic model

    # 初始化 MCP 工具包装器，工具名以 server_name__ 为前缀防止命名冲突
    def __init__(self, client: McpClient, server_name: str, tool: McpToolDef) -> None:
        self._client = client
        self._server_name = server_name
        self._tool = tool
        self.name = f"{server_name}__{tool.name}"
        self.description = tool.description or f"MCP tool from {server_name}"
        self.input_schema: dict[str, Any] = tool.input_schema

    # 调用 MCP server 上的工具；连接不可用时返回错误结果
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        try:
            content = await self._client.call_tool(self._tool.name, dict(params))
        except McpServerUnavailableError:
            return ToolResult(
                content=f"mcp server '{self._server_name}' unavailable",
                is_error=True,
                error_type="runtime_error",
            )
        return ToolResult(content=content)
