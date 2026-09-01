from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from mcp_types import CallToolResult, TextContent, Tool

from weavecode.core.config import McpServerConfig
from weavecode.core.mcp.client import McpSessionHandle
from weavecode.core.mcp.tool import McpTool


def _make_handle() -> AsyncMock:
    return AsyncMock(spec=McpSessionHandle)


def _make_tool(
    tool_name: str = "read_file",
    server_name: str = "filesystem",
) -> tuple[McpTool, AsyncMock]:
    handle = _make_handle()
    tool_def = Tool(
        name=tool_name,
        description=f"Read a file via {server_name}",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
    )
    tool = McpTool(handle, server_name, tool_def)
    return tool, handle


# 功能：McpTool.invoke 应调用 session.call_tool 并将 text 内容封装为 ToolResult
# 设计：mock handle.call_tool 返回标准 CallToolResult，验证 ToolResult.content 一致
@pytest.mark.asyncio
async def test_invoke_calls_mcp_client() -> None:
    tool, handle = _make_tool()
    handle.call_tool = AsyncMock(
        return_value=CallToolResult(content=[TextContent(type="text", text="file content here")])
    )
    result = await tool.invoke({"path": "/tmp/test.txt"})
    assert not result.is_error
    assert result.content == "file content here"
    handle.call_tool.assert_called_once_with("read_file", {"path": "/tmp/test.txt"})


# 功能：工具名应以 {server_name}__ 为前缀防止命名冲突
# 设计：验证 McpTool.name 格式为 "filesystem__read_file"
def test_tool_name_prefixed() -> None:
    tool, _ = _make_tool("read_file", "filesystem")
    assert tool.name == "filesystem__read_file"


# 功能：server 返回 is_error=True 时应包装为 ToolResult.is_error=True
# 设计：mock call_tool 返回 is_error=True 的 CallToolResult，断言 is_error 与内容
@pytest.mark.asyncio
async def test_is_error_result() -> None:
    tool, handle = _make_tool()
    handle.call_tool = AsyncMock(
        return_value=CallToolResult(
            content=[TextContent(type="text", text="boom")],
            is_error=True,
        )
    )
    result = await tool.invoke({"path": "/tmp/x.txt"})
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "boom" in result.content


# 功能：client 抛异常时应返回 runtime_error 类型的 ToolResult
# 设计：mock handle.call_tool 抛 RuntimeError，断言 ToolResult 被正确包装
@pytest.mark.asyncio
async def test_runtime_error_caught() -> None:
    tool, handle = _make_tool()
    handle.call_tool = AsyncMock(side_effect=RuntimeError("unexpected failure"))
    result = await tool.invoke({"path": "/tmp/y.txt"})
    assert result.is_error
    assert result.error_type == "runtime_error"
    assert "unexpected failure" in result.content


# 功能：input_schema 应直接使用 MCP tool_def 中的 schema，而非 pydantic model
# 设计：验证 params_model 为 None，input_schema 与 tool_def.input_schema 一致
def test_input_schema_from_tool_def() -> None:
    tool, _ = _make_tool()
    assert McpTool.params_model is None
    assert "path" in tool.input_schema.get("properties", {})


# 功能：connect_http 在配置了 headers 时注入认证头（创建带 headers 的 httpx client）
# 设计：mock streamable_http_client，断言其收到带 headers 的 http_client 参数
@patch("weavecode.core.mcp.client.streamable_http_client")
@patch("weavecode.core.mcp.client.httpx2.AsyncClient")
async def test_connect_http_injects_headers(
    mock_httpx: AsyncMock, mock_transport: AsyncMock
) -> None:
    mock_transport.return_value.__aenter__ = AsyncMock(return_value=(object(), object()))
    mock_transport.return_value.__aexit__ = AsyncMock(return_value=False)
    mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_httpx.return_value)
    mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

    cfg = McpServerConfig(
        name="exa",
        url="https://mcp.exa.ai/mcp",
        headers={"Authorization": "Bearer secret"},
    )
    # 绕过真正的 initialize（ClientSession 需要真实 transport），只验证 httpx client 被创建
    with patch(
        "weavecode.core.mcp.client.ClientSession",
        autospec=True,
    ) as mock_session:
        mock_session.return_value.initialize = AsyncMock()
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_session.return_value)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
        await McpSessionHandle.connect_http(cfg)

    mock_httpx.assert_called_once_with(headers={"Authorization": "Bearer secret"})


# 功能：connect_http 无 headers 时使用默认 transport（不创建自定义 httpx client）
# 设计：mock streamable_http_client，断言调用时 http_client 参数为 None（默认创建）
@patch("weavecode.core.mcp.client.streamable_http_client")
async def test_connect_http_no_headers_uses_default(
    mock_transport: AsyncMock,
) -> None:
    mock_transport.return_value.__aenter__ = AsyncMock(return_value=(object(), object()))
    mock_transport.return_value.__aexit__ = AsyncMock(return_value=False)

    cfg = McpServerConfig(name="exa", url="https://mcp.exa.ai/mcp")
    with patch(
        "weavecode.core.mcp.client.ClientSession",
        autospec=True,
    ) as mock_session:
        mock_session.return_value.initialize = AsyncMock()
        mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_session.return_value)
        mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
        await McpSessionHandle.connect_http(cfg)

    # 无 headers → 不传 http_client（SDK 内部默认创建）
    call_kwargs = mock_transport.call_args.kwargs
    assert "http_client" not in call_kwargs or call_kwargs["http_client"] is None