from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from weavecode.core.bus.events import StepFinishedEvent, StepStartedEvent
from weavecode.core.compact.budget import shrink_to_fit
from weavecode.core.compact.tokens import RequestProjector, estimate_request
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm import model_table
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.provider import _SYSTEM_PROMPT
from weavecode.core.tools.invocation import invoke_tool
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.compact.compactor import Compactor

log = logging.getLogger(__name__)

# 系统提示词基础段由 provider 持有，循环直接导入复用，避免同一份文本出现两处拷贝

# 终止条件只有三条：
#   LLM 返回 end_turn            → success
#   步数达到 max_steps            → failed: exceeded_max_steps
#   LLM 调用抛错 / 被 Ctrl+C 取消 → failed: llm_error / cancelled
# 工具执行出错不终止：错误作为结果回填，让模型自己换方案


def _now() -> str:
    return datetime.now(UTC).isoformat()


class AgentLoop:
    # 初始化循环所需依赖：LLM provider、工具注册表、事件总线，以及可选的会话与压缩依赖
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        session_id: str = "",
        permission_manager: Any = None,
        compactor: Compactor | None = None,
        auto_compact: bool = True,
        reserve_tokens: int = 20_000,
        model: str = "",
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        # 会话标识与权限管理器由 runner 注入，工具调用前先过审批
        self._session_id = session_id
        self._permission_manager = permission_manager
        # 压缩在发请求前预判：装不下才压，压缩器缺席时循环照常跑
        self._compactor = compactor
        self._auto_compact = auto_compact
        # 本次输出 + 估算容差的预留量，预算 = 模型窗口 - 预留
        self._reserve_tokens = reserve_tokens
        self._model = model
        self._projector = RequestProjector()


    # 驱动 think → tool → observe 闭环，直到模型收工或步数用尽
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            log.debug("step %d start run_id=%s", context.step, context.run_id)
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            system = context.system_prompt(_SYSTEM_PROMPT)
            tools = self._registry.tool_schemas()

            # 发请求前预判本次输入装不装得下；装不下先压缩，保证请求不被截断
            await self._ensure_fits(context, system, tools)

            # think：把当前历史交给 LLM，让它决定下一步做什么
            sent_count = len(context.messages)
            try:
                response = await self._provider.chat(
                    messages=context.messages,
                    tool_schemas=tools,
                    bus=self._bus,
                    run_id=context.run_id,
                    step=context.step,
                    system=system,
                )
            except asyncio.CancelledError:
                # 必须向上传播，让上层有机会在文件关闭后收尾
                context.mark_failed("cancelled")
                raise
            except Exception:
                log.exception("LLM call failed run_id=%s step=%d", context.run_id, context.step)
                context.mark_failed("llm_error")
                break

            # 用真实 usage 校准下一次预判：纯字符估算对中文只有数量级精度
            if response.usage is not None:
                self._projector.observe(response.usage.input_tokens, sent_count)

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
                # 并发执行本轮所有工具调用；gather 按传入顺序返回，结果与调用一一配对
                results = await asyncio.gather(
                    *[
                        invoke_tool(
                            self._registry, tc, self._bus, context.run_id, **invoke_extra
                        )
                        for tc in response.tool_calls
                    ]
                )
                for tc, result in zip(response.tool_calls, results):
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

    # 发请求前预判：输入 + 输出预留装不下就先压缩；压缩不成再确定性降级，绝不硬发
    async def _ensure_fits(
        self,
        context: ExecutionContext,
        system: str,
        tools: list[dict[str, Any]],
    ) -> None:
        if self._compactor is None or not self._auto_compact:
            return
        window = model_table.context_window(self._model)
        budget = window - self._reserve_tokens
        if budget <= 0:
            return
        projected = self._projector.project(system, context.messages, tools)
        if projected <= budget:
            return

        log.info(
            "pre-request compaction run_id=%s step=%d projected≈%d budget=%d window=%d",
            context.run_id, context.step, projected, budget, window,
        )
        result = await self._compactor.compact(context, self._provider)
        self._projector.reset()

        if result is None:
            context.messages = shrink_to_fit(context.messages, budget)
            log.warning(
                "compaction failed, deterministically shrank history run_id=%s step=%d",
                context.run_id, context.step,
            )
            return

        if estimate_request(system, context.messages, tools) > budget:
            context.messages = shrink_to_fit(context.messages, budget)
            log.warning(
                "compaction insufficient, deterministically shrank history run_id=%s step=%d",
                context.run_id, context.step,
            )
