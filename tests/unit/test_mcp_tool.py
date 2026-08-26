from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from weavecode.core.mcp.client import McpClient, McpServerUnavailableError, McpToolDef
from weavecode.core.mcp.tool import McpTool


def _make_handle() -> AsyncMock:
    return AsyncMock(spec=McpClient)


def _make_tool(
    tool_name: str = "read_file",
    server_name: str = "filesystem",
) -> tuple[McpTool, AsyncMock]:
    handle = _make_handle()
    tool_def = McpToolDef(
        name=tool_name,
        description=f"Read a file via {server_name}",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    tool = McpTool(handle, server_name, tool_def)
    return tool, handle


# 功能：McpTool.invoke 应调用 MCP 客户端并把返回文本封装为 ToolResult
# 设计：mock handle.call_tool 返回纯文本，验证 ToolResult.content 一致
@pytest.mark.asyncio
async def test_invoke_calls_mcp_client() -> None:
    tool, handle = _make_tool()
    handle.call_tool = AsyncMock(return_value="file content here")
    result = await tool.invoke({"path": "/tmp/test.txt"})
    assert not result.is_error
    assert result.content == "file content here"
    handle.call_tool.assert_called_once_with("read_file", {"path": "/tmp/test.txt"})


# 功能：工具名应以 {server_name}__ 为前缀防止命名冲突
# 设计：验证 McpTool.name 格式为 "filesystem__read_file"
def test_tool_name_prefixed() -> None:
    tool, _ = _make_tool("read_file", "filesystem")
    assert tool.name == "filesystem__read_file"


# 功能：server 不可用时应包装为 is_error=True 的 ToolResult
# 设计：mock call_tool 抛 McpServerUnavailableError，断言 is_error、错误类别与提示文案
@pytest.mark.asyncio
async def test_is_error_result() -> None:
    tool, handle = _make_tool()
    handle.call_tool = AsyncMock(side_effect=McpServerUnavailableError("connection lost"))
    result = await tool.invoke({"path": "/tmp/x.txt"})
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "unavailable" in result.content


# 功能：input_schema 应直接使用 MCP tool_def 中的 schema，而非 pydantic model
# 设计：验证 params_model 为 None，input_schema 与 tool_def.input_schema 一致
def test_input_schema_from_tool_def() -> None:
    tool, _ = _make_tool()
    assert McpTool.params_model is None
    assert "path" in tool.input_schema.get("properties", {})
