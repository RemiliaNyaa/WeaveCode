from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from weavecode.core.bus.events import (
    PermissionDeniedEvent,
    PermissionGrantedEvent,
    PermissionRequestedEvent,
    ToolCallFailedEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
)
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import ToolCallBlock
from weavecode.core.tools.base import ToolResult
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.permissions.manager import PermissionManager

_DEFAULT_TIMEOUT: float = 120.0


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 发布 ToolCallFailedEvent 并返回对应 ToolResult
async def _fail(
    bus: EventBus,
    run_id: str,
    tool_call: ToolCallBlock,
    error_class: str,
    error_message: str,
    elapsed_ms: int,
) -> ToolResult:
    await bus.publish(
        ToolCallFailedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            error_class=error_class,
            error_message=error_message,
            elapsed_ms=elapsed_ms,
            ts=_now(),
        )
    )
    return ToolResult(content=error_message, is_error=True, error_type=error_class)


# 构造拒绝回馈给模型的文案：讲清这是用户的决定，不是系统故障
def _denial_message(decision: str) -> str:
    return (
        f"The user rejected this tool call (decision: {decision}).\n"
        "This is a deliberate decision by the user, NOT a tool or system failure:\n"
        "- Do NOT retry the same call or try to bypass it.\n"
        "- If it is essential, explain why and ask the user for permission.\n"
        "- Otherwise, try a different approach."
    )


# 校验参数、检查权限、限时调用工具、发布进度事件，失败时转成 ToolResult 回填（不抛异常）
async def invoke_tool(
    registry: ToolRegistry,
    tool_call: ToolCallBlock,
    bus: EventBus,
    run_id: str,
    timeout: float = _DEFAULT_TIMEOUT,
    *,
    permission_manager: PermissionManager | None = None,
    session_id: str = "",
) -> ToolResult:
    t0 = time.monotonic()

    await bus.publish(
        ToolCallStartedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            ts=_now(),
        )
    )

    def elapsed() -> int:
        return int((time.monotonic() - t0) * 1000)

    tool = registry.get(tool_call.name)
    if tool is None:
        return await _fail(
            bus, run_id, tool_call,
            "runtime_error", f"unknown tool: {tool_call.name}", elapsed(),
        )

    if tool.params_model is not None:
        try:
            tool.params_model.model_validate(dict(tool_call.input))
        except ValidationError as exc:
            return await _fail(
                bus, run_id, tool_call,
                "schema_error", str(exc), elapsed(),
            )

    if permission_manager is not None:
        async def _emit_permission(raw: dict[str, Any]) -> None:
            await bus.publish(PermissionRequestedEvent(**raw, run_id=run_id))

        allowed, decision = await permission_manager.check_and_wait(
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            params=dict(tool_call.input),
            session_id=session_id,
            event_emitter=_emit_permission,
        )
        if allowed:
            if decision != "auto_allow":
                await bus.publish(
                    PermissionGrantedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
        else:
            if decision != "auto_deny":
                await bus.publish(
                    PermissionDeniedEvent(
                        run_id=run_id,
                        tool_use_id=tool_call.id,
                        decision=decision,
                        ts=_now(),
                    )
                )
            # 拒绝不终止对话循环：以工具结果形式回填，让模型知晓并改换方案
            return await _fail(
                bus, run_id, tool_call,
                "permission_denied",
                _denial_message(decision),
                elapsed(),
            )

    try:
        result = await asyncio.wait_for(
            tool.invoke(dict(tool_call.input)), timeout=timeout
        )
        ms = elapsed()

        if result.is_error:
            error_class = result.error_type or "runtime_error"
            return await _fail(bus, run_id, tool_call, error_class, result.content, ms)
    except TimeoutError:
        return await _fail(
            bus, run_id, tool_call,
            "timeout", f"tool timed out after {timeout}s", elapsed(),
        )
    except Exception as exc:
        return await _fail(bus, run_id, tool_call, "runtime_error", str(exc), elapsed())

    await bus.publish(
        ToolCallFinishedEvent(
            run_id=run_id,
            tool_use_id=tool_call.id,
            tool_name=tool_call.name,
            elapsed_ms=ms,
            output=result.content,
            ts=_now(),
        )
    )
    return result
