from __future__ import annotations

import asyncio
import datetime
import fnmatch
import logging
import signal
import sys
import time
from datetime import UTC
from pathlib import Path
from typing import Any

from pydantic import BaseModel

import weavecode
from weavecode.core.bus.commands import (
    AgentRunCommand,
    AgentRunResult,
    EventSubscribeCommand,
    EventSubscribeResult,
    PermissionRespondCommand,
    PermissionRespondResult,
    PongResult,
    RulesFileView,
    RulesShowCommand,
    RulesShowResult,
    SessionCloseCommand,
    SessionCloseResult,
    SessionCompactCommand,
    SessionCompactResult,
    SessionCreateCommand,
    SessionCreateResult,
    SessionGetHistoryCommand,
    SessionGetHistoryResult,
    SessionSendMessageCommand,
    SessionSendMessageResult,
)
from weavecode.core.bus.envelope import EventPushEnvelope
from weavecode.core.config import WeaveConfig, get_config
from weavecode.core.events.bus import EventBus
from weavecode.core.events.writer import read_events
from weavecode.core.llm.provider import AnthropicProvider
from weavecode.core.logging_setup import setup_logging
from weavecode.core.mcp.server import McpServerManager
from weavecode.core.permissions.manager import PermissionManager
from weavecode.core.permissions.storage import load_policy
from weavecode.core.runner import AgentRunner
from weavecode.core.runs import new_run_id
from weavecode.core.session import SessionManager, SessionStore
from weavecode.core.storage import Database, apply_migrations
from weavecode.core.trace.record import TraceRecord
from weavecode.core.trace.writer import TraceWriter
from weavecode.core.transport.ipc_broadcaster import IpcEventBroadcaster
from weavecode.core.transport.socket_server import SocketServer, get_connection_writer

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


class CoreApp:
    def __init__(self) -> None:
        self._start_time = time.monotonic()
        self._bus = EventBus()
        self._broadcaster: IpcEventBroadcaster | None = None
        self._trace: TraceWriter | None = None
        self._config: WeaveConfig | None = None
        self._running_runs: set[asyncio.Task[Any]] = set()
        self._sessions: SessionManager | None = None
        self._permission_manager: PermissionManager | None = None
        self._mcp_manager: McpServerManager | None = None
        self._db: Database | None = None

    # 处理 core.ping 请求，返回服务版本、运行时长和接收时间
    async def _ping_handler(self, params: dict[str, Any]) -> PongResult:
        client = params.get("client", "unknown")
        logger.debug("ping from %s", client)
        return PongResult(
            server_version=weavecode.__version__,
            uptime_ms=int((time.monotonic() - self._start_time) * 1000),
            received_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )

    # 将 EventBus 事件写入 trace（作为 EventBus 订阅者）
    async def _trace_event_handler(self, event: BaseModel) -> None:
        assert self._trace is not None
        event_dict = event.model_dump()
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE",
                layer="event",
                kind="event",
                run_id=event_dict.get("run_id"),
                data=event_dict,
            )
        )

    # 启动一次 agent run：异步创建 AgentRunner 并立即返回 run_id
    async def _agent_run_handler(self, params: dict[str, Any]) -> AgentRunResult:
        assert self._sessions is not None
        cmd = AgentRunCommand.model_validate(params)
        session = await self._sessions.create(
            mode="one_shot", title=cmd.goal[:40], working_dir=cmd.cwd
        )
        run_id = new_run_id()
        run_task = asyncio.create_task(
            self._sessions.send_message(session.id, cmd.goal, run_id=run_id)
        )
        self._running_runs.add(run_task)
        run_task.add_done_callback(self._running_runs.discard)
        return AgentRunResult(run_id=run_id)

    # 创建 chat 或 one_shot session，并返回 session_id
    async def _session_create_handler(self, params: dict[str, Any]) -> SessionCreateResult:
        assert self._sessions is not None
        cmd = SessionCreateCommand.model_validate(params)
        session = await self._sessions.create(
            mode=cmd.mode, title=cmd.title, working_dir=cmd.cwd
        )
        return SessionCreateResult(session_id=session.id, status=session.status)

    # 向 session 发送一条用户消息并同步等待对应 run 完成
    async def _session_send_handler(self, params: dict[str, Any]) -> SessionSendMessageResult:
        assert self._sessions is not None
        cmd = SessionSendMessageCommand.model_validate(params)
        run_id = await self._sessions.send_message(cmd.session_id, cmd.content)
        return SessionSendMessageResult(run_id=run_id)

    # 返回 session 的完整 Anthropic messages 历史
    async def _session_history_handler(self, params: dict[str, Any]) -> SessionGetHistoryResult:
        assert self._sessions is not None
        cmd = SessionGetHistoryCommand.model_validate(params)
        messages = await self._sessions.get_history(cmd.session_id)
        return SessionGetHistoryResult(messages=messages)

    # 接收客户端权限审批响应，resolve 对应挂起的 Future
    async def _permission_respond_handler(self, params: dict[str, Any]) -> PermissionRespondResult:
        cmd = PermissionRespondCommand.model_validate(params)
        logger.info(
            "permission.respond received tool_use_id=%s decision=%s",
            cmd.tool_use_id, cmd.decision,
        )
        if self._permission_manager is None:
            logger.error("permission.respond: PermissionManager not initialized")
            return PermissionRespondResult()
        self._permission_manager.respond(cmd.tool_use_id, cmd.decision)
        return PermissionRespondResult()

    # 手动压缩 session thread，将摘要持久化写入 thread.jsonl
    async def _session_compact_handler(self, params: dict[str, Any]) -> SessionCompactResult:
        assert self._sessions is not None
        cmd = SessionCompactCommand.model_validate(params)
        result = await self._sessions.compact(cmd.session_id, cmd.focus)
        return result  # type: ignore[no-any-return]

    # 返回全局与项目 AGENTS.md 的预览（供 TUI 的 /rules 命令显示）
    async def _rules_show_handler(self, params: dict[str, Any]) -> RulesShowResult:
        from weavecode.core.memory.loader import (
            MAX_RULE_LINES,
            PREVIEW_BYTES,
            PREVIEW_LINES,
            global_rules_path,
            preview_rules,
            project_rules_path,
        )

        cmd = RulesShowCommand.model_validate(params)
        cwd = cmd.cwd or str(Path.cwd())
        previews = [
            preview_rules(global_rules_path(), "global"),
            preview_rules(project_rules_path(cwd), "project"),
        ]
        return RulesShowResult(
            files=[
                RulesFileView(
                    scope=p.scope,
                    path=p.path,
                    exists=p.exists,
                    total_lines=p.total_lines,
                    total_bytes=p.total_bytes,
                    shown_lines=p.shown_lines,
                    content=p.content,
                    truncated=p.truncated,
                )
                for p in previews
            ],
            inject_line_limit=MAX_RULE_LINES,
            preview_line_limit=PREVIEW_LINES,
            preview_byte_limit=PREVIEW_BYTES,
        )

    # 关闭 session 并返回 closed 状态
    async def _session_close_handler(self, params: dict[str, Any]) -> SessionCloseResult:
        assert self._sessions is not None
        cmd = SessionCloseCommand.model_validate(params)
        await self._sessions.close(cmd.session_id)
        return SessionCloseResult(status="closed")

    # 注册客户端事件订阅，可选先回放某 run 的历史事件再接收实时流
    async def _subscribe_handler(self, params: dict[str, Any]) -> EventSubscribeResult:
        cmd = EventSubscribeCommand.model_validate(params)
        writer = get_connection_writer()

        replayed_count = 0
        if cmd.replay_from_run is not None:
            replayed_count = await self._replay_events(
                cmd.replay_from_run, writer, cmd.topics
            )

        assert self._broadcaster is not None
        sub_id = self._broadcaster.subscribe(writer, cmd.topics, cmd.scope)
        return EventSubscribeResult(subscription_id=sub_id, replayed_count=replayed_count)

    # 从 event 表向 writer 回放匹配 topic 的历史事件，返回已回放条数
    async def _replay_events(
        self,
        run_id: str,
        writer: asyncio.StreamWriter,
        topics: list[str],
    ) -> int:
        if self._db is None:
            return 0

        count = 0
        for event in await read_events(self._db, run_id):
            event_type = str(event.get("type", ""))
            if not any(fnmatch.fnmatch(event_type, p) for p in topics):
                continue
            envelope = EventPushEnvelope(event=event)
            writer.write(envelope.model_dump_json().encode() + b"\n")
            count += 1

        if count:
            await writer.drain()
        return count

    # 启动守护进程：加载配置、初始化日志、启动 trace、启动 TCP 服务器，并等待退出信号
    async def run(self) -> None:
        self._start_time = time.monotonic()
        self._config = get_config()
        setup_logging(self._config)

        if self._config.trace.enabled:
            trace_path = Path(self._config.trace.file).expanduser()
            self._trace = TraceWriter(trace_path)
            await self._trace.start()
            self._bus.subscribe(self._trace_event_handler)

        self._broadcaster = IpcEventBroadcaster(trace=self._trace)
        self._bus.subscribe(self._broadcaster.handle)
        # 打开 SQLite 并跑迁移；旧的文件式存储已作废（见《数据存储迁移到 SQLite：执行计划》）
        self._db = Database()
        ran = await apply_migrations(self._db)
        if ran:
            logger.info("db: applied %d migration(s): %s", len(ran), ran)
        store = SessionStore(self._db)

        # 持久化权限规则从 permission 表读（原来是 ~/.weave/policy.toml）；
        # 先读出来再交给 manager，manager 的构造函数因此不再做 I/O
        saved_rules = await load_policy(self._db)
        self._permission_manager = PermissionManager(
            db=self._db,
            saved=saved_rules,
            timeout_s=self._config.permission.timeout_s,
        )
        logger.info(
            "permission manager: timeout_s=%.1f  persistent=%d entries",
            self._config.permission.timeout_s,
            len(saved_rules),
        )

        assert self._config is not None
        compact_provider = AnthropicProvider(
            self._config.llm.default_model,
            max_retries=self._config.llm.stream_retries,
        )

        # MCP manager 先建对象（run 的 registry 工厂要靠它取工具），但**不在这里连 server** ——
        # 连接挪到 server.start() 之后，理由见那段注释
        self._mcp_manager = McpServerManager()

        self._sessions = SessionManager(
            store,
            runner_factory=lambda: AgentRunner(
                self._config,  # type: ignore[arg-type]
                bus=self._bus,
                trace=self._trace,
                permission_manager=self._permission_manager,
                mcp_manager=self._mcp_manager,
                db=self._db,
            ),
            bus=self._bus,
            provider=compact_provider,
            compaction_keep_tokens=self._config.compaction.keep_tokens,
        )

        server = SocketServer(
            self._config.host,
            self._config.port,
            self._broadcaster,
            trace=self._trace,
        )
        server.register("core.ping", self._ping_handler)
        server.register("agent.run", self._agent_run_handler)
        server.register("event.subscribe", self._subscribe_handler)
        server.register("session.create", self._session_create_handler)
        server.register("session.send_message", self._session_send_handler)
        server.register("session.get_history", self._session_history_handler)
        server.register("session.close", self._session_close_handler)
        server.register("permission.respond", self._permission_respond_handler)
        server.register("session.compact", self._session_compact_handler)
        server.register("rules.show", self._rules_show_handler)

        addr = await server.start()
        logger.info("weave-core %s listening addr=%s", weavecode.__version__, addr)
        logger.info("config: %s", self._config)

        # ★ 先 listen 再连 MCP：MCP 是**外部依赖**（可能是远程 server），连它要几次网络往返。
        # 若放在 listen 之前，一个慢/挂的远程 server 会让本地 daemon 迟迟不可连（实测 7~25s）。
        # 放在这里：端口先可用、客户端能立刻连上；await 期间事件循环照常处理请求。
        if self._config.mcp.servers:
            logger.info("mcp: starting %d server(s)", len(self._config.mcp.servers))
            await self._mcp_manager.start_all(
                self._config.mcp.servers,
                refresh_on_notify=self._config.mcp.refresh_on_notify,
            )

        loop = asyncio.get_running_loop()
        shutdown = asyncio.Event()
        # Windows 上 asyncio 不支持 add_signal_handler，降级为 signal.signal
        if sys.platform == "win32":
            signal.signal(signal.SIGINT, lambda *_: shutdown.set())
            signal.signal(signal.SIGTERM, lambda *_: shutdown.set())
        else:
            loop.add_signal_handler(signal.SIGINT, shutdown.set)
            loop.add_signal_handler(signal.SIGTERM, shutdown.set)

        await shutdown.wait()

        logger.info("shutting down")
        for run_task in list(self._running_runs):
            run_task.cancel()
        if self._running_runs:
            await asyncio.gather(*self._running_runs, return_exceptions=True)
        if self._mcp_manager is not None:
            await self._mcp_manager.stop_all()
        await server.stop()
        if self._trace is not None:
            await self._trace.stop()


# 同步入口：启动 CoreApp 事件循环
def run() -> None:
    asyncio.run(CoreApp().run())
