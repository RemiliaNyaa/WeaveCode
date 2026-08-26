from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from weavecode.core.config import McpServerConfig
from weavecode.core.mcp.client import McpClient, McpToolDef
from weavecode.core.mcp.server import McpServerManager
from weavecode.core.mcp.tool import McpTool


def _fake_handle(tool_names: list[str], server_name: str) -> AsyncMock:
    handle = AsyncMock(spec=McpClient)
    handle.list_tools = AsyncMock(
        return_value=[
            McpToolDef(
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


# 功能：不解析 type 字段，有 url 解析为 http 配置、否则解析为 stdio 配置；type 被忽略
# 设计：构造三种配置（有 url / 有 command / 显式 type + url），断言 url 和 command 是否正确解析
def test_apply_json_url_or_command_inference() -> None:
    from weavecode.core.config import WeaveConfig, _apply_toml

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
    _apply_toml(config, data)
    by_name = {s.name: s for s in config.mcp.servers}
    assert by_name["sentry"].url == "https://mcp.sentry.dev/mcp"
    assert by_name["filesystem"].command == "npx"
    assert by_name["filesystem"].args == ["-y", "fs"]
    assert by_name["explicit"].url == "https://x/mcp"
