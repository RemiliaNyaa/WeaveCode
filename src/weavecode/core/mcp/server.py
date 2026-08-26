from __future__ import annotations

import logging

from weavecode.core.config import McpServerConfig
from weavecode.core.mcp.client import McpClient
from weavecode.core.mcp.tool import McpTool
from weavecode.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、关闭
class McpServerManager:
    def __init__(self) -> None:
        # server 名 → 连接
        self._clients: dict[str, McpClient] = {}
        # 已发现的工具，按发现顺序拍平存放
        self._tools: list[McpTool] = []

    # 依次连接每个 MCP server，发现工具后缓存；单个 server 失败只记日志跳过
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            try:
                client = await self._connect(cfg)
                tool_defs = await client.list_tools()
                for tool_def in tool_defs:
                    self._tools.append(McpTool(client, cfg.name, tool_def))
                self._clients[cfg.name] = client
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered",
                    cfg.name,
                    len(tool_defs),
                )
            except Exception:
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # 将所有已发现的 MCP 工具注册到指定 registry
    def register_tools(self, registry: ToolRegistry) -> None:
        for tool in self._tools:
            registry.register(tool)

    # 返回所有已发现的 MCP 工具列表，供 runner 每次 run 时注入新 registry
    def get_tools(self) -> list[McpTool]:
        return list(self._tools)

    # 关闭所有 MCP 连接
    async def stop_all(self) -> None:
        for cfg_name in list(self._clients):
            client = self._clients.pop(cfg_name)
            try:
                await client.close()
                log.info("mcp: server '%s' closed", cfg_name)
            except Exception:
                log.warning("mcp: error closing server '%s'", cfg_name)
        self._tools.clear()

    # 按配置的 transport 建立连接：stdio 起子进程，tcp 连已有进程的裸端口
    async def _connect(self, cfg: McpServerConfig) -> McpClient:
        if cfg.transport == "tcp":
            return await McpClient.connect_tcp(cfg.host, cfg.port)
        return await McpClient.connect_stdio(cfg.command, cfg.args, cfg.env)
