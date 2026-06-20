from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int
    # 命中 prompt caching 的输入 token，用来观察缓存断点是否生效
    cache_read_input_tokens: int = 0
    # 本次请求新建缓存写入的输入 token
    cache_creation_input_tokens: int = 0


@dataclass
class ToolCallBlock:
    id: str
    name: str
    input: dict[str, object]


@dataclass
class LlmResponse:
    stop_reason: str  # "end_turn" | "tool_use"
    tool_calls: list[ToolCallBlock] = field(default_factory=list)
    text: str = ""
    usage: UsageStats | None = None
    # thinking blocks from extended thinking — must be preserved verbatim in conversation history
    thinking_blocks: list[dict[str, object]] = field(default_factory=list)
