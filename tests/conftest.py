from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator

import pytest

# daemon 启动超时。取值比"干净环境"需要的（约 1s）宽松很多，因为：
#   ① daemon 自身要 import anthropic / mcp / tree-sitter，约 2s；
#   ② 部分机器（Windows + WSL/Hyper-V/安全软件）上，连一个「没人监听的本地端口」
#      要等约 2s 才返回 ConnectionRefusedError，而不是瞬时拒绝——这会让
#      SocketServer.start() 的探活、以及本 fixture 的轮询都变慢。
_DAEMON_START_TIMEOUT_S = 15.0


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port  # socket released; daemon can bind to this port


@pytest.fixture
async def running_daemon(free_port: int) -> AsyncGenerator[subprocess.Popen[bytes], None]:
    env = os.environ.copy()
    env["WEAVE_PORT"] = str(free_port)
    env["WEAVE_LOG_FILE"] = ""
    env["WEAVE_LOG_LEVEL"] = "WARNING"

    proc = subprocess.Popen([sys.executable, "-m", "weavecode.core"], env=env)

    deadline = time.monotonic() + _DAEMON_START_TIMEOUT_S
    while time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", free_port)
            writer.close()
            await writer.wait_closed()
            break
        except (ConnectionRefusedError, OSError):
            pass
    else:
        proc.terminate()
        proc.wait()
        pytest.fail(f"Daemon did not start within {_DAEMON_START_TIMEOUT_S:.0f} seconds")

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
