from __future__ import annotations

import asyncio
import datetime
import logging
import signal
import sys
import time
from typing import Any

import weavecode
from weavecode.core.bus.commands import PongResult
from weavecode.core.bus.envelope import INVALID_PARAMS, HandlerError
from weavecode.core.config import WeaveConfig, get_config
from weavecode.core.logging_setup import setup_logging
from weavecode.core.runner import AgentRunner
from weavecode.core.runs import new_run_id
from weavecode.core.transport.socket_server import SocketServer

logger = logging.getLogger(__name__)


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._config: WeaveConfig | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=weavecode.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 启动一次 agent run：校验目标后在守护进程内跑完整个任务，返回本次 run 标识
    async def _agent_run_handler(self, params: dict[str, Any]) -> dict[str, str]:
        goal = str(params.get("goal", "")).strip()
        if not goal:
            raise HandlerError(INVALID_PARAMS, "goal is required")
        run_id = new_run_id()
        runner = AgentRunner(self._config)
        await runner.run(goal, run_id=run_id)
        return {"run_id": run_id}

    # 启动守护进程：加载配置、初始化日志、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        server = SocketServer(self._config.host, self._config.port)
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)

        addr = await server.start()
        logger.info("weave-core %s listening addr=%s", weavecode.__version__, addr)
        logger.info("config: %s", self._config)

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Windows 上 asyncio 不支持 add_signal_handler，降级为 signal.signal
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda *_: shutdown.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
        else:
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        await server.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
