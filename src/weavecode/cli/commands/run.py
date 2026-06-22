from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from weavecode.core.config import WeaveConfig


# 接收 dict 格式的事件并将运行进度格式化打印到终端
class StdoutPrinter:
    def __init__(self) -> None:
        self._inline = False  # True while LLM tokens are mid-line
        self._run_start: float = 0.0

    # 若当前行有未换行的 token，补一个换行符
    def _ensure_newline(self) -> None:
        if self._inline:
            print()
            self._inline = False

    # 根据事件 type 字段分发并格式化打印到 stdout/stderr
    def handle(self, event: dict[str, Any]) -> None:
        t = event.get("type", "")

        if t == "run.started":
            self._run_start = time.monotonic()
            print(f"[run] {event.get('run_id', '')}")

        elif t == "step.started":
            self._ensure_newline()
            print(f"[step {event.get('step')}] planning...")

        elif t == "llm.token":
            print(event.get("token", ""), end="", flush=True)
            self._inline = True

        elif t == "tool.call_started":
            self._ensure_newline()
            params_str = json.dumps(event.get("params", {}), ensure_ascii=False)
            print(f"[tool] {event.get('tool_name', '')} {params_str}")

        elif t == "tool.call_finished":
            print(f"[tool] {event.get('tool_name', '')} ✓  {event.get('elapsed_ms')}ms")

        elif t == "tool.call_failed":
            print(
                f"[tool] {event.get('tool_name', '')} ✗  {event.get('error_message', '')}",
                file=sys.stderr,
            )

        elif t == "step.finished":
            self._ensure_newline()
            print(f"[step {event.get('step')}] done")

        elif t == "run.finished":
            self._ensure_newline()
            elapsed = time.monotonic() - self._run_start
            print(f"[run] {event.get('status', '')}  {event.get('steps')} steps  {elapsed:.1f}s")


# 按 JSON-RPC 外壳写一行请求
async def _send(
    writer: asyncio.StreamWriter, req_id: str, method: str, params: dict[str, Any]
) -> None:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
    await writer.drain()


# 同一条连接上既有事件推送也有命令响应，按外壳分流后返回 (kind, payload)
def _decode(raw: dict[str, Any]) -> tuple[str, Any]:
    if raw.get("kind") == "event":
        return "event", raw.get("event") or {}
    if "error" in raw:
        return "error", raw["error"]
    return "result", raw.get("result")


# 异步核心：连接 daemon，订阅事件，触发 run，等 run.finished
async def _run_async(goal: str, config: WeaveConfig) -> int:
    try:
        reader, writer = await asyncio.open_connection(config.host, config.port)
    except (ConnectionRefusedError, OSError):
        print(f"error: core not running ({config.host}:{config.port})", file=sys.stderr)
        return 1

    printer = StdoutPrinter()
    exit_code = 0

    try:
        await _send(
            writer,
            "cli-sub",
            "event.subscribe",
            {"topics": ["run.*", "step.*", "tool.*", "llm.token"], "scope": "global"},
        )
        await _send(writer, "cli-run", "agent.run", {"goal": goal})

        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue

            kind, payload = _decode(raw)
            if kind == "error":
                code = payload.get("code") if isinstance(payload, dict) else None
                message = payload.get("message") if isinstance(payload, dict) else payload
                print(f"error: {code} {message}", file=sys.stderr)
                exit_code = 1
                break
            if kind != "event" or not isinstance(payload, dict):
                continue

            printer.handle(payload)
            if payload.get("type") == "run.finished":
                if payload.get("status") != "success":
                    exit_code = 1
                break
    finally:
        writer.close()
        await writer.wait_closed()

    return exit_code


# 执行 weave run --goal "..." 命令
def cmd_run(goal: str, config: WeaveConfig) -> None:
    try:
        exit_code = asyncio.run(_run_async(goal, config))
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(exit_code)
