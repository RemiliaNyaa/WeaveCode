from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from weavecode.core.permissions.policy import (
    _UNKNOWN_TOOL_DEFAULT,
    DEFAULT_POLICIES,
    PermissionDecision,
    ToolPolicy,
    param_preview,
)
from weavecode.core.permissions.storage import POLICY_FILE, load_policy, save_policy_file

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str


# 管理工具调用权限：硬规则 → 缓存 → 软规则 + 用户审批挂起 + 超时
class PermissionManager:
    def __init__(
        self,
        policies: dict[str, ToolPolicy] | None = None,
        *,
        policy_file: str | None = POLICY_FILE,
        timeout_s: float = 60.0,
    ) -> None:
        self._policies: dict[str, ToolPolicy] = policies or dict(DEFAULT_POLICIES)
        # tool_use_id → pending Future + metadata
        self._pending: dict[str, _PendingRequest] = {}
        # (session_id, tool_name) → "allow"/"deny"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # tool_name → "allow"/"deny"（规则文件持久化，跨会话生效）
        self._persistent_always: dict[str, str] = (
            load_policy(policy_file) if policy_file else {}
        )
        self._policy_file = policy_file
        self._timeout_s = timeout_s

    # 对工具名 + 参数执行硬/软规则评估（无缓存、无挂起）
    def evaluate(self, tool_name: str, params: dict[str, Any]) -> PermissionDecision:
        policy = self._policies.get(tool_name)
        if policy is None:
            return _UNKNOWN_TOOL_DEFAULT
        hard = _evaluate_hard(tool_name, params, policy)
        if hard is not None:
            return hard
        return _evaluate_soft(tool_name, params, policy)

    # 检查权限；需要 ask 时向客户端发事件并等待 respond
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[bool, str]:
        policy = self._policies.get(tool_name)
        if policy is None:
            # 未登记工具（如 MCP 工具）：保守询问
            return await self._ask_and_wait(
                tool_use_id, tool_name, params, session_id, event_emitter
            )

        # 硬规则：黑名单与越界，不可被任何缓存绕过
        hard = _evaluate_hard(tool_name, params, policy)
        if hard == PermissionDecision.DENY:
            return False, "auto_deny"
        if hard == PermissionDecision.ASK:
            return await self._ask_and_wait(
                tool_use_id, tool_name, params, session_id, event_emitter
            )

        # 缓存层：会话级优先，其次持久化（用户「始终」的选择优先于软规则）
        session_key = (session_id, tool_name)
        if session_key in self._session_always:
            cached = self._session_always[session_key]
            return cached == "allow", f"auto_{cached}"
        if tool_name in self._persistent_always:
            cached = self._persistent_always[tool_name]
            return cached == "allow", f"auto_{cached}"

        # 软规则：白名单与默认策略，可被用户的选择覆盖
        soft = _evaluate_soft(tool_name, params, policy)
        if soft == PermissionDecision.ALLOW:
            return True, "auto_allow"
        if soft == PermissionDecision.DENY:
            return False, "auto_deny"

        return await self._ask_and_wait(
            tool_use_id, tool_name, params, session_id, event_emitter
        )

    # ASK 挂起：登记 Future、发审批事件、等 respond 或超时
    async def _ask_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> tuple[bool, str]:
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name,
        )

        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": params,
                "param_preview": param_preview(tool_name, params),
                "session_id": session_id,
                "ts": _now(),
            }
        )

        try:
            raw = await asyncio.wait_for(future, timeout=self._timeout_s)
        except TimeoutError:
            self._pending.pop(tool_use_id, None)
            logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
            return False, "timeout"

        allowed = self._apply_response(raw, session_id, tool_name)
        return allowed, raw

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(self, tool_use_id: str, decision: str) -> None:
        req = self._pending.pop(tool_use_id, None)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return
        if not req.future.done():
            req.future.set_result(decision)

    # 应用审批决策：先记会话内存再回写规则文件；返回是否放行
    def _apply_response(self, decision: str, session_id: str, tool_name: str) -> bool:
        allow = decision in ("allow_once", "always_allow")
        if decision in ("always_allow", "always_deny"):
            value = "allow" if allow else "deny"
            self._session_always[(session_id, tool_name)] = value
            self._persistent_always[tool_name] = value
            if self._policy_file is not None:
                save_policy_file(self._persistent_always, self._policy_file)
        return allow

    # 客户端断连时拒绝该 session 所有待审批请求，防止 Future 永久挂起
    def cancel_session(self, session_id: str, reason: str = "client_disconnected") -> None:
        to_cancel = [
            uid for uid, req in self._pending.items()
            if req.session_id == session_id
        ]
        for uid in to_cancel:
            req = self._pending.pop(uid)
            if not req.future.done():
                logger.debug(
                    "permission: cancel pending tool_use_id=%s reason=%s", uid, reason
                )
                req.future.set_result("deny_once")


# 硬规则包装：延迟导入避免循环依赖
def _evaluate_hard(
    tool_name: str, params: dict[str, Any], policy: ToolPolicy
) -> PermissionDecision | None:
    from weavecode.core.permissions.policy import evaluate_hard
    return evaluate_hard(tool_name, params, policy)


# 软规则包装：延迟导入避免循环依赖
def _evaluate_soft(
    tool_name: str, params: dict[str, Any], policy: ToolPolicy
) -> PermissionDecision:
    from weavecode.core.permissions.policy import evaluate_soft
    return evaluate_soft(tool_name, params, policy)
