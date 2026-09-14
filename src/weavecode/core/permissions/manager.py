from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from weavecode.core.permissions.paths import approval_dir, extract_paths, is_within
from weavecode.core.permissions.policy import (
    _UNKNOWN_TOOL_DEFAULT,
    DEFAULT_POLICIES,
    PermissionDecision,
    PermissionResult,
    ToolPolicy,
    param_preview,
)
from weavecode.core.permissions.storage import (
    POLICY_FILE,
    Rule,
    load_policy,
    save_policy_file,
)

logger = logging.getLogger(__name__)

# 横切越界权限的权限名（与工具无关，只看路径）
EXTERNAL_DIRECTORY = "external_directory"


def _now() -> str:
    return datetime.datetime.now(UTC).isoformat()


@dataclass
class _PendingRequest:
    future: asyncio.Future[str]
    session_id: str
    tool_name: str
    permission: str
    resource: str
    project: str
    # 「始终允许」时要落盘的资源（可能多个）
    save_resources: list[str] = field(default_factory=list)


# 管理工具调用权限：越界横切检查 + 工具策略评估 + 用户审批挂起 + 会话级/持久化 always 缓存 + 超时
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
        # (session_id, permission) → "allow"（session 内存，重启丢失）
        self._session_always: dict[tuple[str, str], str] = {}
        # 持久化规则：(project, permission, resource)，从规则文件加载
        self._saved: list[Rule] = list(load_policy(policy_file)) if policy_file else []
        self._policy_file = policy_file
        # 0 表示不超时
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

    # 检查权限；越界或需 ask 时向客户端发事件并等待响应
    async def check_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        working_dir: str = "",
    ) -> PermissionResult:
        # ① 越界横切检查：路径在工作目录之外 → 询问（与工具无关、目录级粒度）
        if working_dir:
            resource = self._first_outside_dir(tool_name, params, working_dir)
            if resource is not None and not self._is_saved(
                working_dir, EXTERNAL_DIRECTORY, resource
            ):
                result = await self._ask_and_wait(
                    tool_use_id, tool_name, params, session_id, event_emitter,
                    permission=EXTERNAL_DIRECTORY,
                    resource=resource,
                    project=working_dir,
                )
                if not result.allowed:
                    return result

        # ② 工具权限检查
        policy = self._policies.get(tool_name)
        if policy is None:
            return await self._ask_and_wait(
                tool_use_id, tool_name, params, session_id, event_emitter,
                permission=tool_name, resource="*", project=working_dir,
            )

        hard = _evaluate_hard(tool_name, params, policy)
        if hard == PermissionDecision.DENY:
            return PermissionResult(False, "auto_deny", tool_name, "")
        if hard == PermissionDecision.ASK:
            # 黑名单命中的命令：强制审批，且排在缓存之前 → 不可被「始终允许」绕过
            return await self._ask_and_wait(
                tool_use_id, tool_name, params, session_id, event_emitter,
                permission=tool_name, resource="*", project=working_dir,
            )

        # 缓存层：会话级优先，其次持久化（用户「始终允许」优先于软规则）
        if (session_id, tool_name) in self._session_always:
            return PermissionResult(True, "auto_allow", tool_name, "*")
        if self._is_saved(working_dir, tool_name, "*"):
            return PermissionResult(True, "auto_allow", tool_name, "*")

        soft = _evaluate_soft(tool_name, params, policy)
        if soft == PermissionDecision.ALLOW:
            return PermissionResult(True, "auto_allow", tool_name, "*")
        if soft == PermissionDecision.DENY:
            return PermissionResult(False, "auto_deny", tool_name, "*")

        return await self._ask_and_wait(
            tool_use_id, tool_name, params, session_id, event_emitter,
            permission=tool_name, resource="*", project=working_dir,
        )

    # 返回第一个位于工作目录之外的路径对应的「批准目录」；无越界返回 None
    def _first_outside_dir(
        self, tool_name: str, params: dict[str, Any], working_dir: str
    ) -> str | None:
        for path in extract_paths(tool_name, params, working_dir):
            if not is_within(path, working_dir):
                return approval_dir(path)
        return None

    # 查询持久化规则是否已覆盖 (project, permission, resource)
    def _is_saved(self, project: str, permission: str, resource: str) -> bool:
        for proj, perm, res in self._saved:
            if proj != project or perm != permission:
                continue
            # 越界：请求目录位于已批准目录之内即命中（目录级包含）
            if permission == EXTERNAL_DIRECTORY:
                if is_within(resource, res):
                    return True
            elif res == resource:
                return True
        return False

    # ASK 挂起：登记 Future、发审批事件、等 respond 或超时
    async def _ask_and_wait(
        self,
        tool_use_id: str,
        tool_name: str,
        params: dict[str, Any],
        session_id: str,
        event_emitter: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        permission: str,
        resource: str,
        project: str,
    ) -> PermissionResult:
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending[tool_use_id] = _PendingRequest(
            future=future,
            session_id=session_id,
            tool_name=tool_name,
            permission=permission,
            resource=resource,
            project=project,
            save_resources=[resource] if resource and resource != "*" else [],
        )

        await event_emitter(
            {
                "type": "permission.requested",
                "tool_use_id": tool_use_id,
                "tool_name": tool_name,
                "params": params,
                "param_preview": param_preview(tool_name, params),
                "session_id": session_id,
                "permission": permission,
                "resource": resource,
                "ts": _now(),
            }
        )

        try:
            if self._timeout_s > 0:
                raw = await asyncio.wait_for(future, timeout=self._timeout_s)
            else:
                raw = await future
        except TimeoutError:
            self._pending.pop(tool_use_id, None)
            logger.info("permission: timeout tool_use_id=%s tool=%s", tool_use_id, tool_name)
            return PermissionResult(False, "timeout", permission, resource)

        allowed = self._apply_response(raw, session_id, permission, project, resource)
        return PermissionResult(allowed, raw, permission, resource)

    # 处理客户端返回的审批决策，resolve 对应 Future
    def respond(self, tool_use_id: str, decision: str) -> None:
        req = self._pending.pop(tool_use_id, None)
        if req is None:
            logger.warning("permission.respond: unknown tool_use_id=%s", tool_use_id)
            return
        if not req.future.done():
            req.future.set_result(decision)

    # 应用审批决策：更新会话缓存与持久化规则；返回是否放行
    def _apply_response(
        self, decision: str, session_id: str, permission: str, project: str, resource: str
    ) -> bool:
        allow = decision in ("allow_once", "always_allow")
        if decision != "always_allow":
            return allow

        # 会话级缓存：本会话内同权限直接放行
        self._session_always[(session_id, permission)] = "allow"
        # 持久化：仅当有明确资源（越界目录）或工具级 "*" 时记录
        save_resource = resource or "*"
        rule = (project, permission, save_resource)
        if rule not in self._saved:
            self._saved.append(rule)
        if self._policy_file is not None:
            try:
                save_policy_file(self._saved, self._policy_file)
                logger.info(
                    "permission: always allow saved permission=%s resource=%s project=%s",
                    permission, save_resource, project,
                )
            except Exception:
                logger.exception(
                    "permission: failed to write policy file path=%s", self._policy_file
                )
        else:
            logger.warning("permission: policy_file is None, skipping persistence")
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
                req.future.set_result("reject_once")


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
