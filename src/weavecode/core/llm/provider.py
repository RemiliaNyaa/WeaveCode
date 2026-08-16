from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import anthropic

from weavecode.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from weavecode.core.events.bus import EventBus
from weavecode.core.llm import model_table
from weavecode.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

# 单次请求的输出上限
_DEFAULT_MAX_TOKENS = 8_192

# tool_result 超过这个字符数就截断，只保留 keep 部分
TOOL_RESULT_LIMIT = 8_000
TOOL_RESULT_KEEP = 4_000

_SYSTEM_PROMPT = (
    "You are Weave, an AI assistant running in a terminal. "
    "Use the provided tools to make progress on the user's goal. "
    "When the goal is reached, answer with the final result."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


# 对消息列表里超长的 tool_result 做内存截断，返回新列表，历史原样不动
def truncate_tool_results(
    messages: list[dict[str, object]],
    limit: int = TOOL_RESULT_LIMIT,
    keep: int = TOOL_RESULT_KEEP,
) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for message in messages:
        if message.get("role") != "user":
            result.append(message)
            continue
        content = message.get("content")
        if not isinstance(content, list):
            result.append(message)
            continue
        new_blocks: list[object] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and isinstance(block.get("content"), str)
            ):
                text = str(block["content"])
                if len(text) > limit:
                    omitted = len(text) - keep
                    block = dict(block)
                    block["content"] = (
                        text[:keep]
                        + f"\n[... {omitted} chars omitted. Full output in run events.]"
                    )
            new_blocks.append(block)
        result.append({**message, "content": new_blocks})
    return result


class AnthropicProvider:
    # 初始化 Anthropic 客户端；client 可在测试时注入以跳过 API key 检查
    def __init__(self, model: str, client: Any = None) -> None:
        if client is None:
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise SystemExit("ANTHROPIC_API_KEY not set")
            self._client: Any = anthropic.AsyncAnthropic(api_key=api_key)
        else:
            self._client = client
        self._model = model

    # 流式调用 Anthropic API，逐 token 发布事件并返回 LlmResponse
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

        # system 文本打上缓存断点：一次 run 里前缀不变，第二步起就能命中缓存
        system_blocks: list[dict[str, object]] = [
            {
                "type": "text",
                "text": system or _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        # 工具 schema 随请求注入；断点加在最后一项，整个工具列表作为一个缓存前缀
        tools: list[dict[str, object]] = list(tool_schemas)
        if tools:
            last = dict(tools[-1])
            last["cache_control"] = {"type": "ephemeral"}
            tools = tools[:-1] + [last]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system_blocks,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        text_parts: list[str] = []
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                text_parts.append(text)
            final_message = await stream.get_final_message()

        usage = final_message.usage
        cache_read: int = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create: int = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # 上下文水位：本轮输入占模型窗口的比例，随用量事件下发给客户端显示
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
                thinking_blocks.append(
                    {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
                )

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
