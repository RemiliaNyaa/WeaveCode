from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import anthropic

from weavecode.core.bus.events import LlmModelSelectedEvent, LlmTokenEvent, LlmUsageEvent
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, ToolCallBlock, UsageStats

# 单次请求的输出上限
_DEFAULT_MAX_TOKENS = 8_192

_SYSTEM_PROMPT = (
    "You are Weave, an AI assistant running in a terminal. "
    "Use the provided tools to make progress on the user's goal. "
    "When the goal is reached, answer with the final result."
)


# 返回当前 UTC 时间的 ISO 8601 字符串
def _now() -> str:
    return datetime.now(UTC).isoformat()


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
        bus: EventBus,
        run_id: str,
        *,
        step: int = 0,
    ) -> LlmResponse:
        await bus.publish(
            LlmModelSelectedEvent(run_id=run_id, model=self._model, strategy="static", ts=_now())
        )

        system_blocks: list[dict[str, object]] = [{"type": "text", "text": _SYSTEM_PROMPT}]

        kwargs: dict[str, object] = {
            "model": self._model,
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system_blocks,
            "messages": messages,
        }

        text_parts: list[str] = []
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                await bus.publish(LlmTokenEvent(run_id=run_id, token=text, ts=_now()))
                text_parts.append(text)
            final_message = await stream.get_final_message()

        usage = final_message.usage
        await bus.publish(
            LlmUsageEvent(
                run_id=run_id,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
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
            ),
        )
