from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageStats:
    input_tokens: int
    output_tokens: int


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
