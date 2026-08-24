from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


# 连不上 server、握手失败或调用超时都会落到这个异常上
class McpServerUnavailableError(Exception):
    pass


# server 报上来的单个工具定义
class McpToolDef:
    def __init__(
        self, name: str, description: str = "", input_schema: dict[str, Any] | None = None
    ) -> None:
        self.name = name
        self.description = description
        self.input_schema = input_schema or {"type": "object", "properties": {}}


# 行分隔的 JSON-RPC 传输：一行一条消息，按请求 id 配对响应，写入加锁保证不交叉
class _LineTransport:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._closed = False

    # 逐行读消息：响应按 id 投给等待中的请求，server 主动推送的通知这一版先忽略
    async def read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(payload, dict) or "id" not in payload:
                    continue
                future = self._pending.get(payload["id"])
                if future is None or future.done():
                    continue
                if isinstance(payload.get("error"), dict):
                    future.set_exception(
                        McpServerUnavailableError(
                            f"mcp: {payload['error'].get('message') or 'request failed'}"
                        )
                    )
                else:
                    future.set_result(payload.get("result"))
        except (ConnectionError, OSError):
            pass
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpServerUnavailableError("mcp: connection closed"))
            self._pending.clear()

    # 发一条请求并等到同 id 的响应
    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
            return await asyncio.wait_for(future, timeout=60.0)
        except TimeoutError as exc:
            raise McpServerUnavailableError(f"mcp: no response to '{method}'") from exc
        finally:
            self._pending.pop(request_id, None)

    # 发一条通知（不期待响应）
    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _write(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            if self._closed:
                raise McpServerUnavailableError("mcp: connection closed")
            self._writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
            try:
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                raise McpServerUnavailableError(f"mcp: write failed: {exc}") from exc

    async def close(self) -> None:
        self._closed = True
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            log.debug("mcp: error closing transport", exc_info=True)


# 手写的 MCP 客户端：握手、列工具、调用工具都在这一层完成
class McpClient:
    def __init__(self, transport: _LineTransport) -> None:
        self._transport = transport
        self._proc: asyncio.subprocess.Process | None = None
        self._read_task = asyncio.create_task(transport.read_loop())

    # 以 stdio 起一个 server 子进程并完成握手
    @classmethod
    async def connect_stdio(
        cls,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> McpClient:
        argv = [command, *(args or [])]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                env={**os.environ, **env} if env else None,
            )
        except (OSError, ValueError) as exc:
            raise McpServerUnavailableError(f"mcp: cannot start '{command}': {exc}") from exc
        if proc.stdout is None or proc.stdin is None:
            raise McpServerUnavailableError(f"mcp: '{command}' has no stdio pipes")
        client = cls(_LineTransport(proc.stdout, proc.stdin))
        client._proc = proc
        await client.initialize()
        return client

    # 连一个已经起好的裸 TCP 端口（对端同样是行分隔 JSON-RPC）
    @classmethod
    async def connect_tcp(cls, host: str, port: int) -> McpClient:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as exc:
            raise McpServerUnavailableError(f"mcp: cannot reach {host}:{port}: {exc}") from exc
        client = cls(_LineTransport(reader, writer))
        await client.initialize()
        return client

    # initialize 握手 + initialized 通知，之后这条连接才可用
    async def initialize(self) -> None:
        await self._transport.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "weave", "version": "0.0.1"},
            },
        )
        await self._transport.notify("notifications/initialized")

    # 列出 server 暴露的工具
    async def list_tools(self) -> list[McpToolDef]:
        result = await self._transport.request("tools/list") or {}
        defs: list[McpToolDef] = []
        for raw in result.get("tools", []):
            if not isinstance(raw, dict):
                continue
            schema = raw.get("inputSchema")
            defs.append(
                McpToolDef(
                    str(raw.get("name", "")),
                    str(raw.get("description") or ""),
                    schema if isinstance(schema, dict) else None,
                )
            )
        return defs

    # 调用 server 上的工具，返回可以直接回填给模型的文本
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        result = (
            await self._transport.request("tools/call", {"name": name, "arguments": arguments})
            or {}
        )
        parts: list[str] = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(parts)

    # 关闭连接；stdio 起的子进程一并收掉
    async def close(self) -> None:
        await self._transport.close()
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await proc.wait()
            except (ProcessLookupError, OSError):
                log.debug("mcp: error stopping server process", exc_info=True)
        self._read_task.cancel()
