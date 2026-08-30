from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from weavecode.core.bus.envelope import HandlerError
from weavecode.core.bus.events import (
    SessionClosedEvent,
    SessionCreatedEvent,
    SessionMessageReceivedEvent,
    SessionResumedEvent,
    SessionWaitingForInputEvent,
    SkillInvokedEvent,
)
from weavecode.core.events.bus import EventBus
from weavecode.core.runs import new_run_id
from weavecode.core.session.model import Session, SessionMode
from weavecode.core.session.store import SessionStore
from weavecode.core.skills.loader import SkillLoader
from weavecode.core.task import TaskManager

if TYPE_CHECKING:
    from weavecode.core.llm.base import LLMProvider
    from weavecode.core.runner import AgentRunner

SESSION_NOT_FOUND = -32010
SESSION_CLOSED = -32011
SESSION_BUSY = -32012


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class SessionManager:
    # 初始化会话管理器，接入会话存储、runner 工厂、事件总线和可选的 LLM provider（用于手动压缩）
    def __init__(
        self,
        store: SessionStore,
        runner_factory: Callable[[], AgentRunner],
        bus: EventBus,
        provider: LLMProvider | None = None,
    ) -> None:
        self._store = store
        self._runner_factory = runner_factory
        self._bus = bus
        self._provider = provider
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._task_managers: dict[str, TaskManager] = {}

    # 创建新 session 并写入 meta.json
    async def create(self, mode: SessionMode, title: str = "") -> Session:
        sid = f"sess-{uuid.uuid4().hex[:12]}"
        ts = _now()
        session = Session(
            id=sid,
            mode=mode,
            status="active",
            title=title,
            created_at=ts,
            updated_at=ts,
            run_ids=[],
        )
        self._sessions[sid] = session
        self._locks[sid] = asyncio.Lock()
        self._store.write_meta(session)
        await self._bus.publish(SessionCreatedEvent(session_id=sid, mode=mode, ts=ts))
        return session

    # 处理用户消息，追加 thread 并启动一次 agent run
    async def send_message(self, sid: str, content: str, *, run_id: str | None = None) -> str:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")

        async with lock:
            if session.status == "closed":
                raise HandlerError(SESSION_CLOSED, "session already closed")

            if session.status == "waiting_for_input":
                await self._bus.publish(SessionResumedEvent(session_id=sid, ts=_now()))

            run_id = run_id or new_run_id()
            session.run_ids.append(run_id)
            session.updated_at = _now()
            self._store.write_meta(session)

            # Skill 解析：检测 "/" 前缀。命中时存一条指令消息并发布 skill 调用事件，
            # 未命中则存原始消息，按普通对话继续。
            skill = None
            arguments = ""
            if content.startswith("/"):
                parts = content[1:].split(None, 1)
                skill_name = parts[0]
                arguments = parts[1] if len(parts) > 1 else ""
                skill = SkillLoader().resolve(skill_name)
                if skill is not None:
                    self._store.append_message(sid, "user", skill.user_prompt(arguments))
                    await self._bus.publish(
                        SkillInvokedEvent(
                            skill_name=skill_name,
                            arguments=arguments,
                            run_id=run_id,
                            ts=_now(),
                        )
                    )
                else:
                    self._store.append_message(sid, "user", content)
            else:
                self._store.append_message(sid, "user", content)
            await self._bus.publish(
                SessionMessageReceivedEvent(session_id=sid, content=content, ts=_now())
            )

            if not session.title:
                session.title = content[:40]

            # 手动触发 skill 时把参数文本作为本次 run 的目标，指令消息留给 agent 自己读
            goal = arguments if skill is not None else content

            runner = self._runner_factory()
            # 复用本 session 缓存的任务管理器实例（同 session 跨 run 复用同一份任务状态）
            task_manager = self._task_managers.get(sid)
            if task_manager is None:
                task_manager = TaskManager(self._store.session_dir(sid) / ".tasks")
                self._task_managers[sid] = task_manager
            await runner.run_and_capture(
                goal,
                run_id=run_id,
                session=session,
                store=self._store,
                task_manager=task_manager,
            )

            session.updated_at = _now()
            if session.mode == "one_shot":
                session.status = "closed"
                await self._bus.publish(
                    SessionClosedEvent(session_id=sid, ts=session.updated_at)
                )
            else:
                session.status = "waiting_for_input"
                await self._bus.publish(
                    SessionWaitingForInputEvent(
                        session_id=sid,
                        last_run_id=run_id,
                        ts=session.updated_at,
                    )
                )
            self._store.write_meta(session)
            return run_id

    # 关闭指定 session 并更新 meta.json
    async def close(self, sid: str) -> None:
        session = self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        async with lock:
            session.status = "closed"
            session.updated_at = _now()
            self._store.write_meta(session)
            self._task_managers.pop(sid, None)  # 关闭会话时释放缓存的任务管理器
            await self._bus.publish(SessionClosedEvent(session_id=sid, ts=session.updated_at))

    # 手动压缩指定 session 的 thread，将摘要持久化写入 thread.jsonl
    async def compact(self, sid: str, focus: str = "") -> Any:
        self._get_session(sid)
        lock = self._locks[sid]
        if lock.locked():
            raise HandlerError(SESSION_BUSY, "session busy")
        if self._provider is None:
            raise HandlerError(-32020, "provider not available for compaction")
        async with lock:
            from weavecode.core.bus.commands import SessionCompactResult
            from weavecode.core.compact.compactor import Compactor

            messages = self._store.read_messages(sid)
            compactor = Compactor(self._bus, self._store.session_dir(sid), sid)
            result = await compactor.compact_messages(messages, self._provider, focus=focus)
            if result is None:
                raise HandlerError(-32021, "compaction failed or not beneficial")
            compacted = result.as_messages()
            self._store.write_compacted(sid, compacted)
            from weavecode.core.compact.tokens import estimate_messages

            return SessionCompactResult(
                summary_tokens=result.summary_tokens,
                saved_tokens=max(
                    0, result.original_token_estimate - estimate_messages(compacted)
                ),
            )

    # 读取指定 session 的完整 thread 历史
    async def get_history(self, sid: str) -> list[dict[str, Any]]:
        self._get_session(sid)
        return self._store.read_messages(sid)

    # 从内存索引取 session，不存在时抛 JSON-RPC 结构化错误
    def _get_session(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            raise HandlerError(SESSION_NOT_FOUND, "session not found")
        return session
