from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp_types import Tool

from weavecode.core.config import McpServerConfig
from weavecode.core.mcp.client import McpSessionHandle
from weavecode.core.mcp.server import McpServerManager
from weavecode.core.mcp.tool import McpTool


def _fake_handle(tool_names: list[str], server_name: str) -> AsyncMock:
    handle = AsyncMock(spec=McpSessionHandle)
    handle.list_tools = AsyncMock(
        return_value=[
            Tool(
                name=n,
                description=f"{server_name} tool {n}",
                input_schema={},
            )
            for n in tool_names
        ]
    )
    return handle


# 功能：start_all 连接成功后，get_tools 返回拍平后的全部工具列表
# 设计：两个不同名的 server，断言工具以 {server}__{tool} 形式全部出现
@pytest.mark.asyncio
async def test_start_all_collects_tools_from_multiple_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_connect(self, cfg: McpServerConfig) -> AsyncMock:
        if cfg.name == "global_srv":
            return _fake_handle(["time", "file"], cfg.name)
        return _fake_handle(["weather"], cfg.name)

    monkeypatch.setattr(McpServerManager, "_connect", fake_connect)
    manager = McpServerManager()
    servers = [
        McpServerConfig(name="global_srv", url="http://127.0.0.1:3000"),
        McpServerConfig(name="other_srv", url="http://127.0.0.1:3001"),
    ]
    await manager.start_all(servers)
    tools = manager.get_tools()
    names = {t.name for t in tools}
    assert names == {"global_srv__time", "global_srv__file", "other_srv__weather"}


# 功能：同名 server 出现时整组替换——只保留后连接（项目本地）的工具，旧工具整体移除
# 设计：同 name 连两次（全局先、项目后），断言工具只有项目本地那组，且旧连接被关闭
@pytest.mark.asyncio
async def test_same_name_server_replaces_entire_tool_group(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []

    async def fake_connect(self, cfg: McpServerConfig) -> AsyncMock:
        if cfg.name == "demo":
            handle = _fake_handle(["time", "file"], "demo")
            handle.close = AsyncMock(side_effect=lambda: closed.append("demo"))
            return handle
        raise AssertionError("unexpected server")

    monkeypatch.setattr(McpServerManager, "_connect", fake_connect)
    manager = McpServerManager()
    # 全局先、项目本地后（get_config 顺序保证）
    servers = [
        McpServerConfig(name="demo", url="http://10.0.0.1:3000"),   # 全局
        McpServerConfig(name="demo", url="http://127.0.0.1:4000"),  # 项目本地
    ]
    await manager.start_all(servers)
    tools = manager.get_tools()
    names = {t.name for t in tools}
    # 项目本地那组工具完整保留
    assert names == {"demo__time", "demo__file"}
    assert isinstance(tools[0], McpTool)
    # 旧连接被关闭了一次（全局 demo 被替换时关掉）
    assert closed == ["demo"]
    # _handles 只剩一个（项目本地）
    assert list(manager._handles) == ["demo"]


# 功能：不解析 type 字段，有 url 解析为 http 配置、否则解析为 stdio 配置；type 被忽略
# 设计：构造三种配置（有 url / 有 command / 显式 type + url），断言 url 和 command 是否正确解析
def test_apply_json_url_or_command_inference() -> None:
    from weavecode.core.config import WeaveConfig, _apply_json

    config = WeaveConfig()

    data = {
        "mcp": {
            "mcpServers": {
                "sentry": {"url": "https://mcp.sentry.dev/mcp"},            # 有 url → http 配置
                "filesystem": {"command": "npx", "args": ["-y", "fs"]},     # 有 command → stdio 配置
                "explicit": {"type": "http", "url": "https://x/mcp"},       # type 字段被忽略，只看 url
            }
        }
    }
    _apply_json(config, data)
    by_name = {s.name: s for s in config.mcp.servers}
    assert by_name["sentry"].url == "https://mcp.sentry.dev/mcp"
    assert by_name["filesystem"].command == "npx"
    assert by_name["filesystem"].args == ["-y", "fs"]
    assert by_name["explicit"].url == "https://x/mcp"