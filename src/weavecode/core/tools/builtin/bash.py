from __future__ import annotations

import asyncio

from weavecode.core.tools.base import BaseTool, ToolResult

_MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB
_DEFAULT_TIMEOUT = 60


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Execute a shell command and return its output (stdout + stderr combined).\n"
        "Output is truncated at 64 KB.\n"
        "Timeout defaults to 60 seconds, max 120."
    )
    input_schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Maximum seconds to wait (default {_DEFAULT_TIMEOUT}, max 120).",
            },
        },
        "required": ["command"],
    }

    # 在子进程中执行 shell 命令，合并 stdout/stderr，超时中断并把输出截断到 64 KB
    async def invoke(self, params: dict[str, object]) -> ToolResult:
        command = str(params.get("command", ""))
        if not command:
            return ToolResult(content="command is required", is_error=True)
        timeout = int(params.get("timeout", _DEFAULT_TIMEOUT))
        timeout = max(1, min(120, timeout))

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return ToolResult(content=f"[timeout after {timeout}s]", is_error=True)

        output = stdout_bytes.decode("utf-8")
        if len(stdout_bytes) > _MAX_OUTPUT_BYTES:
            output = output[:_MAX_OUTPUT_BYTES] + "\n[truncated]"

        returncode = proc.returncode or 0
        if returncode != 0:
            return ToolResult(content=f"[exit {returncode}]\n{output}", is_error=True)
        return ToolResult(content=output or "[no output]")
