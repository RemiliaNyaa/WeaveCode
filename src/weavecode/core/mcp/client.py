from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.session import MessageHandlerFnT
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ToolListChangedNotification

from weavecode.core.config import McpServerConfig

log = logging.getLogger(__name__)


class McpServerUnavailableError(Exception):
    pass


# 收到 server 的「工具列表已变」通知时**只做标记**，绝不在这里 await 任何 I/O
#
# 必要性：官方 SDK 的 message_handler 是被 dispatcher **内联 await** 的
# （源码注释：Running the handler inline would park the dispatcher's read loop and
#  deadlock handlers that await session I/O）。所以这里绝不能 await list_tools ——
# 只标脏，由 AgentLoop 下一步检查时再重新拉取。
def _make_message_handler(
    on_tools_changed: Callable[[], None] | None,
) -> MessageHandlerFnT:
    async def _handler(message: Any) -> None:
        if isinstance(message, ToolListChangedNotification) and on_tools_changed is not None:
            on_tools_changed()

    return _handler


# 持有官方 SDK 的 session 与底层 transport，统一提供连接、发现、调用、关闭
class McpSessionHandle:
    def __init__(self, session: ClientSession, exit_stack: AsyncExitStack) -> None:
        self._session = session
        self._exit_stack = exit_stack

    @property
    def session(self) -> ClientSession:
        return self._session

    # 建立 stdio 连接（官方 SDK 起子进程 + 握手），返回句柄
    @staticmethod
    async def connect_stdio(
        cfg: McpServerConfig, *, on_tools_changed: Callable[[], None] | None = None
    ) -> McpSessionHandle:
        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=cfg.env or None,
            cwd=cfg.cwd or None,
        )
        return await _open(stdio_client(params), on_tools_changed=on_tools_changed)

    # 建立 http 连接（官方 SDK 连远程端点 + 握手）；有 headers 时注入认证头
    @staticmethod
    async def connect_http(
        cfg: McpServerConfig, *, on_tools_changed: Callable[[], None] | None = None
    ) -> McpSessionHandle:
        if cfg.headers:
            http_client = httpx2.AsyncClient(headers=cfg.headers)
            return await _open(
                streamable_http_client(cfg.url, http_client=http_client),
                extra_cms=[http_client],
                on_tools_changed=on_tools_changed,
            )
        return await _open(
            streamable_http_client(cfg.url), on_tools_changed=on_tools_changed
        )

    # 列出 server 暴露的工具定义（官方 SDK 的 Tool 对象列表）
    async def list_tools(self) -> list[Any]:
        result = await self._session.list_tools()
        return list(result.tools)

    # 调用 server 上的工具，返回官方 SDK 的 CallToolResult（含 content 与 is_error）
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return await self._session.call_tool(name, arguments)

    # 关闭 session 与底层 transport（退出 async context manager 栈）
    async def close(self) -> None:
        try:
            await self._exit_stack.aclose()
        except Exception:
            log.debug("mcp: error closing session", exc_info=True)


# 进入 transport、session 与额外清理项（如自定义 httpx client）的 context manager，初始化后交给句柄
# 注意：extra_cms（如 httpx client）须先于 transport 进入，保证退出顺序为 transport→session→extra
async def _open(
    transport_cm: Any,
    extra_cms: list[Any] | None = None,
    *,
    on_tools_changed: Callable[[], None] | None = None,
) -> McpSessionHandle:
    stack = AsyncExitStack()
    try:
        for cm in extra_cms or []:
            await stack.enter_async_context(cm)
        read, write = await stack.enter_async_context(transport_cm)
        session = ClientSession(
            read, write, message_handler=_make_message_handler(on_tools_changed)
        )
        await stack.enter_async_context(session)
        await session.initialize()
    except BaseException:
        await stack.aclose()
        raise
    return McpSessionHandle(session, stack)
