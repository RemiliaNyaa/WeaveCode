from __future__ import annotations

import logging

from weavecode.core.config import McpServerConfig
from weavecode.core.mcp.client import McpSessionHandle
from weavecode.core.mcp.tool import McpTool
from weavecode.core.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


# 管理所有 MCP server 连接的生命周期：启动、工具发现、注册、关闭
class McpServerManager:
    def __init__(self) -> None:
        # server 名 → 连接句柄（含官方 SDK session）
        self._handles: dict[str, McpSessionHandle] = {}
        # server 名 → 该 server 暴露的 McpTool 列表；同名 server 时整组替换（项目本地覆盖全局）
        self._tools: dict[str, list[McpTool]] = {}

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；同名 server 整组替换并关闭旧连接
    async def start_all(self, servers: list[McpServerConfig]) -> None:
        for cfg in servers:
            try:
                # 同名 server（如全局与项目本地配置同名）时，先关闭旧连接并移除旧工具，实现整体替换
                if cfg.name in self._handles:
                    await self._close_server(cfg.name)
                handle = await self._connect(cfg)
                tool_defs = await handle.list_tools()
                tools = [McpTool(handle, cfg.name, tool_def) for tool_def in tool_defs]
                self._tools[cfg.name] = tools
                self._handles[cfg.name] = handle
                log.info(
                    "mcp: server '%s' connected, %d tool(s) discovered",
                    cfg.name,
                    len(tools),
                )
            except Exception:
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # 将所有已发现的 MCP 工具注册到指定 registry
    def register_tools(self, registry: ToolRegistry) -> None:
        for tools in self._tools.values():
            for tool in tools:
                registry.register(tool)

    # 返回所有已发现的 MCP 工具列表（拍平 dict，供 runner 每次 run 时注入新 registry）
    def get_tools(self) -> list[McpTool]:
        return [tool for tools in self._tools.values() for tool in tools]

    # 关闭所有 MCP 连接
    async def stop_all(self) -> None:
        for name in list(self._handles):
            await self._close_server(name)
        self._handles.clear()
        self._tools.clear()

    # 根据有没有 url 建立连接（官方 SDK；有 url 走 http，否则 stdio）
    async def _connect(self, cfg: McpServerConfig) -> McpSessionHandle:
        if cfg.url:
            return await McpSessionHandle.connect_http(cfg)
        if not cfg.command:
            raise ValueError(f"mcp server '{cfg.name}': stdio requires 'command'")
        return await McpSessionHandle.connect_stdio(cfg)

    # 关闭单个 server 的连接并移除其工具缓存
    async def _close_server(self, name: str) -> None:
        handle = self._handles.pop(name, None)
        if handle is not None:
            try:
                await handle.close()
                log.info("mcp: server '%s' closed", name)
            except Exception:
                log.warning("mcp: error closing server '%s'", name)
        self._tools.pop(name, None)
