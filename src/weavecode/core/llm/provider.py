from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from typing import Any

import anthropic
import httpx

from weavecode.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from weavecode.core.events.bus import EventBus
from weavecode.core.llm import model_table
from weavecode.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

# 单次请求的输出上限默认值；实际取它与模型输出上限的较小者
_DEFAULT_MAX_TOKENS = 8_192

# 流式调用失败后最多重试几次（首发不算，所以最多 _STREAM_MAX_RETRIES + 1 次尝试）
_STREAM_MAX_RETRIES = 5
# 退避起始秒数：第 n 次重试等待 _STREAM_RETRY_BASE_S * 2^(n-1)，即 2 / 4 / 8 / 16 / 32 秒
# （现场计算，不写死元组 —— 重试次数改了退避自动跟着变）
_STREAM_RETRY_BASE_S = 2.0

log = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are Weave, an AI assistant operating in a terminal environment. "
    "You complete the user's goal by taking small, concrete steps and using the "
    "available tools. Work iteratively: understand the task, act, observe results, "
    "and adjust.\n"
    "\n"
    "## Tool usage policy\n"
    "- Prefer the most specific tool for each job. Use read_file/list_dir for "
    "reading and listing, write_file/edit_file for writing and editing, and "
    "bash only for actual system commands (git, tests, package managers). "
    "Do not use bash to read or edit files when the dedicated tools exist.\n"
    "- Search for files with glob (by name pattern) and grep (by content regex); "
    "prefer them over running rg/grep through bash. Pass an absolute path "
    "directory to narrow the search.\n"
    "- When multiple tool calls are independent, call them in parallel in a single "
    "step to save time. Only call tools sequentially when one result is needed "
    "to construct the next call.\n"
    "- Read before you write: before overwriting a file, check whether it exists "
    "and read it first to avoid destroying content. Prefer edit_file for small "
    "changes; use write_file for new files or full replacements.\n"
    "- Use absolute paths everywhere. Tools reject relative paths.\n"
    "- Think before calling tools: a tool call should serve one clear purpose. "
    "Avoid chains of tiny redundant calls.\n"
    "\n"
    "## Planning\n"
    "- Use update_plan when the task is complex and multi-step: multiple distinct "
    "actions, logical stages, dependencies between steps, or ambiguity that "
    "benefits from a visible roadmap.\n"
    "- Do NOT use update_plan for simple or single-step requests that you can just "
    "do immediately. Do not pad simple work with filler steps.\n"
    "- When a plan exists, update it as you complete steps: mark the finished step "
    "completed, the next step in_progress, at most one in_progress at a time.\n"
    "\n"
    "## Sub-agents\n"
    "- spawn_agent blocks by default: it waits for the sub-agent to finish and returns "
    "the result directly. Set background=true to run the sub-agent in the background "
    "instead - the call returns a run_id immediately.\n"
    "- To run multiple sub-agents in parallel, issue MULTIPLE spawn_agent tool calls in "
    "a single tool-calling step (one call per sub-task). Do NOT wait for one sub-agent "
    "to finish before issuing the next call.\n"
    "- Use background=true only when you have other work to do while the sub-agents "
    "run. Background execution must be requested on EVERY spawn_agent call in that "
    "step: if even one of them is blocking, they all block.\n"
    "- Collect background sub-agents with wait_agent (omit run_ids to wait for all of "
    "them). Every background sub-agent you started must be collected before you finish "
    "your turn.\n"
    "\n"
    "## Finishing\n"
    "- Keep working until the goal is fully achieved. When done, respond with a "
    "final answer summarizing the result and do not call any more tools."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(
        self, model: str, client: Any = None, max_retries: int = _STREAM_MAX_RETRIES
    ) -> None:
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            self._client: Any = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            self._client = client
        self._model = model
        self._max_retries = max_retries

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse；网络中断时自动重试
    async def chat(
        self,
        messages: list[dict[str, object]],
        tool_schemas: list[dict[str, object]],
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
        system: str | None = None,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": min(_DEFAULT_MAX_TOKENS, model_table.max_output(self._model)),
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        final_message: Any = None

        # 最多重试 self._max_retries 次（首发之外）；退避现场计算，不写死元组
        for attempt in range(1, self._max_retries + 2):
            text_parts = []
            try:
                async with self._client.messages.stream(**kwargs) as stream:
                    async for text in stream.text_stream:
                        # Only publish token events on the first attempt to avoid TUI duplicates
                        if attempt == 1:
                            await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                        text_parts.append(text)
                    final_message = await stream.get_final_message()
                break  # success
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as exc:
                if attempt > self._max_retries:
                    log.error(
                        "stream failed after %d attempts run_id=%s step=%d: %s",
                        attempt, run_id, step, exc,
                    )
                    raise
                delay = _STREAM_RETRY_BASE_S * (2 ** (attempt - 1))
                log.warning(
                    "stream dropped (attempt %d/%d) run_id=%s step=%d: %s — retrying in %.0fs",
                    attempt, self._max_retries + 1, run_id, step, exc, delay,
                )
                await asyncio.sleep(delay)

        assert final_message is not None

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        context_pct = usage.input_tokens / model_table.context_window(self._model)

        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
                ts=_now(),
            )
        )

        tool_calls: list[ToolCallBlock] = []
        thinking_blocks: list[dict[str, object]] = []
        for block in final_message.content:
            if block.type == "tool_use":
                tool_calls.append(
                    ToolCallBlock(id=block.id, name=block.name, input=dict(block.input))
                )
            elif block.type == "thinking":
                # thinking blocks must be passed back verbatim in subsequent requests
                thinking_blocks.append({"type": "thinking", "thinking": block.thinking, "signature": block.signature})

        return LlmResponse(
            stop_reason=final_message.stop_reason or "end_turn",
            tool_calls=tool_calls,
            text="".join(text_parts),
            thinking_blocks=thinking_blocks,
            usage=UsageStats(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_create,
                context_pct=context_pct,
            ),
        )
