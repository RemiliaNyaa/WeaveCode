from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
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
    from weavecode.core.permissions.manager import PermissionManager
    from weavecode.core.subagent.runs import BackgroundRuns, SubagentOutcome


log = logging.getLogger(__name__)

# 派生子 Agent 的工具名：loop 侧做「一步内混用归一化」时要认这个工具
SPAWN_AGENT_TOOL_NAME = "spawn_agent"

# 收工兜底：第一级「提醒模型自己去收」最多几次，第二级「系统接管收齐」最多几次
_MAX_COLLECT_NUDGES = 1
_MAX_AUTO_COLLECTS = 1

# 重复调用闸门：连续命中几次拦截就强制终止 run（第 1、2 次只回填错误，第 3 次终止）
_REPEAT_MAX_HITS = 3

# 被重复闸门拦下时回填给模型的文案（英文，与工具结果的其它错误文案一致）
_REPEAT_BLOCKED_TEXT = (
    "Blocked: this exact tool call (same tool, same arguments) has already been "
    "repeated {n} times in a row. Repeating it will not produce a different result. "
    "Change your approach: use different arguments, use another tool, or explain "
    "why you are stuck."
)


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


# 单个工具调用的签名：工具名 + 规范化参数（键序不影响判定）
def _call_signature(tool_call: ToolCallBlock) -> str:
    try:
        payload = json.dumps(tool_call.input, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(sorted(tool_call.input.items()))
    return f"{tool_call.name}({payload})"


# 一步的调用签名：该步所有工具调用的签名排序后拼接（同一批调用的顺序不影响判定）
def _step_signature(tool_calls: list[ToolCallBlock]) -> str:
    return "\n".join(sorted(_call_signature(tc) for tc in tool_calls))


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
    # 初始化循环所需依赖：LLM provider、工具注册表、事件总线，以及可选的权限管理器、压缩器和 session ID
    def __init__(
        self,
        provider: LLMProvider,
        registry: ToolRegistry,
        bus: EventBus,
        *,
        permission_manager: PermissionManager | None = None,
        compactor: Compactor | None = None,
        auto_compact: bool = True,
        reserve_tokens: int = 20_000,
        model: str = "",
        session_id: str = "",
        working_dir: str = "",
        runs: BackgroundRuns | None = None,
        repeat_limit: int = 3,
        registry_provider: Callable[[], Awaitable[ToolRegistry]] | None = None,
    ) -> None:
        self._provider = provider
        self._registry = registry
        self._bus = bus
        self._permission_manager = permission_manager
        self._compactor = compactor
        self._auto_compact = auto_compact
        self._reserve_tokens = reserve_tokens
        self._model = model
        self._session_id = session_id
        self._working_dir = working_dir
        # 后台子 Agent 登记表（只有主 Agent 有；子 Agent 为 None）
        self._runs = runs
        self._collect_nudges = 0
        self._auto_collects = 0
        # 重复调用闸门状态：上一步的签名 + 连续相同次数 + 本次连续重复期间被拦次数
        self._repeat_limit = repeat_limit
        self._last_step_sig = ""
        self._repeat_streak = 0
        self._repeat_hits = 0
        # 每步重建 registry 的工厂（有它才能让 MCP 工具变化在「下一步」就生效）
        self._registry_provider = registry_provider
        self._projector = RequestProjector()

    # 驱动 plan→act→observe 循环直到上下文终止；CancelledError 向上传播
    async def run(self, context: ExecutionContext) -> None:
        while not context.is_done():
            context.step += 1
            # 每步取一次最新 registry：MCP server 发了「工具列表已变」通知时，通知 handler 只标脏，
            # 这里重建 registry 就会把新工具带上 → 变化在「下一步」生效（没有脏 server 时零 I/O）
            if self._registry_provider is not None:
                self._registry = await self._registry_provider()
            await self._bus.publish(
                StepStartedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

            system = await context.system_prompt_async(_SYSTEM_PROMPT)
            tools = self._registry.tool_schemas()

            # 发请求前预判本次输入装不装得下；装不下先压缩，保证请求正常执行不被截断
            await self._ensure_fits(context, system, tools)

            # [plan] call LLM — API errors terminate the run
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
                context.mark_failed("cancelled")
                raise
            except Exception:
                logging.getLogger(__name__).exception(
                    "LLM call failed run_id=%s step=%d", context.run_id, context.step
                )
                context.mark_failed("llm_error")
                break

            # 用真实 usage 校准下一次预判：纯字符估算对中文只有数量级精度
            if response.usage is not None:
                self._projector.observe(response.usage.input_tokens, sent_count)

            # [observe] append assistant content blocks to context
            # thinking blocks must come first and be preserved verbatim for extended thinking mode
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

            # [act] execute each requested tool; errors become tool results so loop continues
            if response.stop_reason == "tool_use":
                # 重复调用闸门：连续 N 步发出完全相同的调用 → 拦下（第 3 次拦截则终止 run）
                blocked, terminate = self._repeat_gate(response.tool_calls)
                if terminate:
                    text = _REPEAT_BLOCKED_TEXT.format(n=self._repeat_streak)
                    # 仍然补齐 tool_result：保证 assistant(tool_use) 有配对，历史回放不残缺
                    for tc in response.tool_calls:
                        context.add_tool_result(tc.id, text, is_error=True)
                    context.mark_failed("repeat_loop")
                else:
                    # 并行执行本轮所有工具调用（含多个 spawn_agent）；结果按 tool_use_id 配对回填
                    results = await asyncio.gather(
                        *[
                            invoke_tool(
                                self._registry, tc, self._bus, context.run_id,
                                permission_manager=self._permission_manager,
                                session_id=self._session_id,
                                working_dir=self._working_dir,
                            )
                            if tc.id not in blocked
                            else self._blocked_tool_result(context.run_id, tc)
                            for tc in response.tool_calls
                        ]
                    )
                    for tc, result in zip(response.tool_calls, results):
                        context.add_tool_result(tc.id, result.content, is_error=result.is_error)
            elif response.stop_reason == "max_tokens" and response.tool_calls:
                # Output token limit hit mid-tool-call; input is incomplete.
                # Add synthetic error results so the conversation stays balanced.
                for tc in response.tool_calls:
                    context.add_tool_result(
                        tc.id,
                        "Error: output token limit reached before this tool call could be completed. "
                        "Please break the task into smaller steps and try again.",
                        is_error=True,
                    )

            # Termination check — end_turn wins over max_steps if both hit on same step
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
                # 已经因其它原因（如重复调用死循环）失败时，不要覆盖掉原来的 reason
                context.mark_failed("exceeded_max_steps")

            await self._bus.publish(
                StepFinishedEvent(run_id=context.run_id, step=context.step, ts=_now())
            )

    # 重复调用闸门：连续 N 步发出完全相同的工具调用 → 判定为原地打转
    #
    # 状态只有一对「上一步签名 + 连续次数」：相同 +1、不同就替换并清零（模型换路了就重新给机会）。
    # 返回 (被拦下的 tool_use_id 集合, 是否强制终止 run)。
    def _repeat_gate(self, tool_calls: list[ToolCallBlock]) -> tuple[set[str], bool]:
        sig = _step_signature(tool_calls)
        if sig == self._last_step_sig:
            self._repeat_streak += 1
        else:
            self._last_step_sig = sig
            self._repeat_streak = 1
            self._repeat_hits = 0

        if self._repeat_streak < self._repeat_limit:
            return set(), False

        self._repeat_hits += 1
        if self._repeat_hits >= _REPEAT_MAX_HITS:
            return set(), True
        return {tc.id for tc in tool_calls}, False

    # 被重复闸门拦下的调用：不执行，直接回填错误结果；并补发事件让 TUI 当普通工具失败显示
    async def _blocked_tool_result(self, run_id: str, tool_call: ToolCallBlock) -> ToolResult:
        text = _REPEAT_BLOCKED_TEXT.format(n=self._repeat_streak)
        await self._bus.publish(
            ToolCallStartedEvent(
                run_id=run_id,
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                params=dict(tool_call.input),
                ts=_now(),
            )
        )
        await self._bus.publish(
            ToolCallFailedEvent(
                run_id=run_id,
                tool_use_id=tool_call.id,
                tool_name=tool_call.name,
                error_class="repeat_loop",
                error_message=text,
                elapsed_ms=0,
                ts=_now(),
            )
        )
        return ToolResult(content=text, is_error=True, error_type="repeat_loop")

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
