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
        # 「工具列表已变」的 server 名集合（由通知 handler 标脏，下一步检查时重拉）
        self._dirty: set[str] = set()
        # 是否挂通知回调（配置开关 mcp.refreshOnNotify；start_all 时写入）
        self._refresh_on_notify = True

    # 依次连接每个 MCP server，发现工具后缓存供后续 registry 使用；同名 server 整组替换并关闭旧连接
    # refresh_on_notify=False 时不挂「工具列表已变」通知回调，即不随通知刷新
    async def start_all(
        self, servers: list[McpServerConfig], *, refresh_on_notify: bool = True
    ) -> None:
        self._refresh_on_notify = refresh_on_notify
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
                    cfg.name, len(tools),
                )
            except Exception:
                log.exception("mcp: server '%s' failed to start, skipping", cfg.name)

    # 把所有脏 server 的工具清单重新拉一遍并更新缓存
    #
    # 只拉「被通知标脏」的那些（按 server 粒度）；**拉取失败保留旧缓存**，只记 warning。
    # 没有任何脏 server 时立即返回，不做任何 I/O。
    async def refresh_dirty(self) -> bool:
        if not self._dirty:
            return False
        names = [name for name in self._dirty if name in self._handles]
        # 先清空：重拉期间新到的通知会重新标脏，留到下一步处理
        self._dirty.clear()

        refreshed = False
        for name in names:
            handle = self._handles[name]
            try:
                tool_defs = await handle.list_tools()
            except Exception:
                log.warning("mcp: refresh failed for '%s', keeping cached tools", name)
                continue
            self._tools[name] = [McpTool(handle, name, d) for d in tool_defs]
            refreshed = True
            log.info("mcp: server '%s' tools refreshed, %d tool(s)", name, len(self._tools[name]))
        return refreshed

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
        self._dirty.clear()

    # 根据有没有 url 建立连接（官方 SDK；有 url 走 http，否则 stdio）
    # 连接时挂上「工具列表已变」的通知回调：收到通知只标脏，重拉留给 refresh_dirty
    async def _connect(self, cfg: McpServerConfig) -> McpSessionHandle:
        def _mark_dirty() -> None:
            self._dirty.add(cfg.name)

        on_changed = _mark_dirty if self._refresh_on_notify else None
        if cfg.url:
            return await McpSessionHandle.connect_http(cfg, on_tools_changed=on_changed)
        if not cfg.command:
            raise ValueError(f"mcp server '{cfg.name}': stdio requires 'command'")
        return await McpSessionHandle.connect_stdio(cfg, on_tools_changed=on_changed)

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
        self._dirty.discard(name)
