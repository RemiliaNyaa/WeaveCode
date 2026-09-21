from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from weavecode.core.bus.events import (
    StepFinishedEvent,
    StepStartedEvent,
    ToolCallFailedEvent,
    ToolCallStartedEvent,
)
from weavecode.core.compact.budget import shrink_to_fit
from weavecode.core.compact.tokens import RequestProjector, estimate_request
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm import model_table
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.provider import _SYSTEM_PROMPT
from weavecode.core.llm.types import ToolCallBlock
from weavecode.core.tools.base import ToolResult
from weavecode.core.tools.invocation import invoke_tool
from weavecode.core.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from weavecode.core.compact.compactor import Compactor
    from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome

log = logging.getLogger(__name__)

# 派生子 Agent 的工具名：loop 侧做「一步内混用归一化」时要认这个工具
SPAWN_AGENT_TOOL_NAME = "spawn_agent"

# 收工兜底：第一级「提醒模型自己去收」最多几次，第二级「系统接管收齐」最多几次
_MAX_COLLECT_NUDGES = 1
_MAX_AUTO_COLLECTS = 1

# 系统提示词基础段由 provider 持有，循环直接导入复用，避免同一份文本出现两处拷贝

# 终止条件只有三条：
#   LLM 返回 end_turn            → success
#   步数达到 max_steps            → failed: exceeded_max_steps
#   LLM 调用抛错 / 被 Ctrl+C 取消 → failed: llm_error / cancelled
# 工具执行出错不终止：错误作为结果回填，让模型自己换方案


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 一步内的多个 spawn_agent：只要有一个是阻塞（含默认），整步全部改成阻塞
#
# 理由：gather 要等整步的工具调用都返回，非阻塞在同一步里拿不到任何好处，
# 反而会让同一轮出现「内联结果 + run_id」两种返回形态，模型容易困惑。
def _normalize_spawn_modes(tool_calls: list[ToolCallBlock]) -> None:
    spawns = [tc for tc in tool_calls if tc.name == SPAWN_AGENT_TOOL_NAME]
    if len(spawns) < 2:
        return
    if all(bool(tc.input.get("background", False)) for tc in spawns):
        return  # 全部显式 background=true → 保持非阻塞
    for tc in spawns:
        tc.input["background"] = False


# 第一级兜底文案：提醒模型还有后台子 Agent 在跑，不能结束本轮
def _reminder_text(run_ids: list[str]) -> str:
    joined = ", ".join(run_ids)
    return (
        "<system-reminder>\n"
        f"You tried to end your turn, but {len(run_ids)} background sub-agent(s) are still "
        f"running: {joined}.\n"
        "You must collect their results first. Call wait_agent (omit run_ids to wait for "
        "all of them, or pass the run_ids you need) and use the results before you finish.\n"
        "</system-reminder>"
    )


# 第二级兜底文案：系统已替模型收齐后台子 Agent 的结果
def _collected_text(outcomes: list[SubagentOutcome]) -> str:
    lines = [
        "<system-reminder>",
        f"You tried to end your turn while {len(outcomes)} background sub-agent(s) were "
        "still running. The system waited for them and collected the results:",
    ]
    lines += [f"- {o.run_id} ({o.status}): {o.result}" for o in outcomes]
    lines.append("Incorporate these results and finish.")
    lines.append("</system-reminder>")
    return "\n".join(lines)


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
        working_dir: str = "",
        runs: BackgroundRuns | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        # 会话标识与权限管理器由 runner 注入，工具调用前先过审批
        self._session_id = session_id
        self._permission_manager = permission_manager
        self._working_dir = working_dir
        # 压缩在发请求前预判：装不下才压，压缩器缺席时循环照常跑
        self._compactor = compactor
        self._auto_compact = auto_compact
        # 本次输出 + 估算容差的预留量，预算 = 模型窗口 - 预留
        self._reserve_tokens = reserve_tokens
        self._model = model
        # 后台子 Agent 登记表（只有主 Agent 有；子 Agent 为 None）
        self._runs = runs
        self._collect_nudges = 0
        self._auto_collects = 0
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
            # 先归一化本步子 Agent 派发的模式，这样 transcript 记录的就是实际生效的参数
            _normalize_spawn_modes(response.tool_calls)
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
                        "working_dir": self._working_dir,
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

            # 终止检查 —— end_turn 赢过 max_steps，但还有后台子 Agent 时先不放行
            if response.stop_reason == "end_turn":
                pending = self._runs.pending_ids() if self._runs is not None else []
                if pending and self._collect_nudges < _MAX_COLLECT_NUDGES:
                    # 第一级：还有后台子 Agent 在跑 → 提醒模型自己去收，本轮不结束
                    # （追加 user 消息而不改 system prompt：system+tools 是缓存前缀，改了会失效）
                    self._collect_nudges += 1
                    context.add_user_notice(_reminder_text(pending))
                elif pending and self._auto_collects < _MAX_AUTO_COLLECTS:
                    # 第二级：模型还是不收 → 系统替它收齐，结果作为一条消息注入后再给一轮
                    self._auto_collects += 1
                    context.add_user_notice(
                        _collected_text(await self._runs.wait(None))  # type: ignore[union-attr]
                    )
                else:
                    if pending:
                        log.warning(
                            "run ended with %d background sub-agent(s) still running: %s",
                            len(pending), ", ".join(pending),
                        )
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
