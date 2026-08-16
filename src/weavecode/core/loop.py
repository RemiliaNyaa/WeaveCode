from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from weavecode.core.bus.events import StepFinishedEvent, StepStartedEvent
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.provider import truncate_tool_results
from weavecode.core.llm.types import UsageStats
from weavecode.core.tools.invocation import invoke_tool
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.task import TaskManager

log = logging.getLogger(__name__)

# 系统提示词基础段：交给 context 统一拼装，循环只持有这一份文本
_SYSTEM_PROMPT = (
    "You are Weave, a terminal AI assistant. Take small, concrete steps toward the "
    "user's goal, act with the available tools, observe what happens after every "
    "action, and keep going until the goal is reached."
)

# 终止条件只有三条：
#   LLM 返回 end_turn            → success
#   步数达到 max_steps            → failed: exceeded_max_steps
#   LLM 调用抛错 / 被 Ctrl+C 取消 → failed: llm_error / cancelled
# 工具执行出错不终止：错误作为结果回填，让模型自己换方案


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化循环所需依赖：LLM provider、工具注册表、事件总线与任务管理器
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        tasks: TaskManager | None = None,
        session_id: str = "",
        permission_manager: Any = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        # 任务状态由 runner 注入，循环每一步读一次并带给模型与工具链
        self._tasks = tasks
        # 会话标识与权限管理器由 runner 注入，工具调用前先过审批
        self._session_id = session_id
        self._permission_manager = permission_manager
        # 上一轮请求的用量，水位判据用它来决定要不要压缩
        self._last_usage: UsageStats | None = None

    # 把当前任务状态拼进 system prompt：模型每一步都能看到清单走到哪了
    def _task_section(self, system: str) -> str:
        if self._tasks is None:
            return system
        tasks = self._tasks.list()
        if not tasks:
            return system
        lines = [f"- [{task.status}] {task.subject}" for task in tasks]
        return system + "\n\n## Tasks\n" + "\n".join(lines)

    # 驱动 think → tool → observe 闭环，直到模型收工或步数用尽
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            log.debug("step %d start run_id=%s", context.step, context.run_id)
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            # think：把当前历史交给 LLM，让它决定下一步做什么
            # 超长 tool_result 只在这份内存副本里截断，历史原样保留
            outgoing = truncate_tool_results(list(context.messages))
            try:
                response = await self._provider.chat(
                    messages=outgoing,
                    tool_schemas=self._registry.tool_schemas(),
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=self._task_section(context.system_prompt(_SYSTEM_PROMPT)),
                )
            except asyncio.CancelledError:
                # 必须向上传播，让上层有机会在文件关闭后收尾
                context.mark_failed("cancelled")
                raise
            except Exception:
                log.exception("LLM call failed run_id=%s step=%d", context.run_id, context.step)
                context.mark_failed("llm_error")
                break

            # 记录本轮用量：上下文水位随用量事件下发给客户端，这里留下原始数字供压缩判据使用
            if response.usage is not None:
                self._last_usage = response.usage
                log.debug(
                    "context usage run_id=%s step=%d input_tokens=%d",
                    context.run_id,
                    context.step,
                    response.usage.input_tokens,
                )

            # observe：响应先进历史，再执行工具——顺序反过来会破坏消息配对
            blocks: list[dict[str, object]] = list(response.thinking_blocks)
            if response.text:
                blocks.append({"type": "text", "text": response.text})
            for tc in response.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
                )
            context.add_assistant_message(blocks)

            # act：逐个执行工具调用，失败结果同样回填给模型
            if response.stop_reason == "tool_use":
                # 会话标识随权限审批一起下发（审批按会话记缓存）；没接入审批链路时保持最简调用
                invoke_extra: dict[str, Any] = {}
                if self._permission_manager is not None:
                    invoke_extra = {
                        "permission_manager": self._permission_manager,
                        "session_id": self._session_id,
                    }
                for tc in response.tool_calls:
                    result = await invoke_tool(
                        self._registry, tc, self._bus, context.run_id, **invoke_extra
                    )
                    context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # 输出被 token 上限截断，工具调用只有半截：补一条错误结果保持配对完整
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be "
                        "completed. Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # 终止检查：模型收工优先于步数上限
            if response.stop_reason == "end_turn":
                context.result = response.text or ""
                context.mark_success()
            elif context.step >= context.max_steps and not context.is_done():
                context.mark_failed("exceeded_max_steps")

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )
