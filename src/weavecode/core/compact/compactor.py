from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from weavecode.core.compact.tokens import estimate_messages
from weavecode.core.events.bus import EventBus

if TYPE_CHECKING:
    from weavecode.core.context import ExecutionContext
    from weavecode.core.llm.base import LLMProvider

logger = logging.getLogger(__name__)

CHECKPOINT_ACK = "Understood, I'll continue from this summary."

_SUMMARIZER_SYSTEM = "You are a helpful assistant that summarizes conversations."

_COMPACT_PROMPT = """\
Create a summary of the conversation in the <conversation> tags above so another coding \
agent can continue the work.

Structure your response with exactly these six sections:

## 1. Original Goal
One sentence describing what the user asked the agent to accomplish.

## 2. Completed Steps
Bullet list of what has been done. Be specific (file paths, commands run, decisions made).

## 3. Key Constraints & Discoveries
Facts learned during the run that affect future decisions \
(e.g., API limitations, file formats, user preferences stated mid-conversation).

## 4. Current File State
For each file that was created or modified: path, a one-line description of its current state.

## 5. Remaining TODOs
Ordered list of what still needs to be done to complete the original goal.

## 6. Critical Data
Any values the next LLM needs verbatim: IDs, tokens, exact error messages, config values \
discovered during the run.

Be concise. Omit reasoning steps and intermediate attempts. Keep conclusions."""


@dataclass
class CompactionResult:
    summary_text: str
    original_token_estimate: int
    summary_tokens: int

    # 压缩后替换历史用的两条消息（user 摘要 + assistant 确认）
    def as_messages(self) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": self.summary_text},
            {"role": "assistant", "content": CHECKPOINT_ACK},
        ]


# 构造摘要提示词：整段历史放进 <conversation>，再跟上六段式模板
def _build_prompt(material: str, focus: str) -> str:
    blocks = [
        "Here is the conversation so far:\n\n<conversation>\n"
        f"{material}\n</conversation>",
        _COMPACT_PROMPT,
    ]
    if focus.strip():
        blocks.append(f"IMPORTANT: Pay special attention to: {focus.strip()}")
    return "\n\n".join(blocks)


# 将消息列表序列化为可供 LLM 阅读的纯文本
def _messages_to_text(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(f"[{role}]\n{content}")
        elif isinstance(content, list):
            blocks: list[str] = []
            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    blocks.append(block.get("text", ""))
                elif btype == "thinking":
                    blocks.append(f"[thinking]\n{block.get('thinking', '')}")
                elif btype == "tool_use":
                    blocks.append(
                        f"<tool_call name={block.get('name')} id={block.get('id')}>\n"
                        f"{block.get('input', {})}\n</tool_call>"
                    )
                elif btype == "tool_result":
                    blocks.append(
                        f"<tool_result id={block.get('tool_use_id')}>\n"
                        f"{block.get('content', '')}\n</tool_result>"
                    )
            parts.append(f"[{role}]\n" + "\n".join(blocks))
    return "\n\n".join(parts)


class Compactor:
    # 初始化压缩器，绑定事件总线、会话目录与会话标识
    def __init__(self, bus: EventBus, session_dir: str | Path, session_id: str) -> None:
        self._bus = bus
        self._session_dir = Path(session_dir)
        self._session_id = session_id

    # 压缩 ExecutionContext.messages，就地替换为「摘要 + 确认」两条消息并把摘要落盘
    async def compact(
        self,
        context: ExecutionContext,
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        result = await self.compact_messages(context.messages, provider, focus=focus)
        if result is None:
            return None

        context.messages = result.as_messages()
        self._write_summary(result.summary_text)
        logger.info(
            "context compacted session=%s run=%s original≈%d summary=%d tokens",
            self._session_id,
            context.run_id,
            result.original_token_estimate,
            result.summary_tokens,
        )
        return result

    # 纯函数式压缩：接收消息列表，返回 CompactionResult；失败时返回 None
    async def compact_messages(
        self,
        messages: list[dict[str, Any]],
        provider: LLMProvider,
        focus: str = "",
    ) -> CompactionResult | None:
        original_estimate = estimate_messages(messages)
        prompt = _build_prompt(_messages_to_text(messages), focus)

        summarized = await self._summarize(prompt, provider)
        if summarized is None:
            return None
        summary_text, summary_tokens = summarized

        return CompactionResult(
            summary_text=summary_text,
            original_token_estimate=original_estimate,
            summary_tokens=summary_tokens,
        )

    # 调用一次独立 LLM 生成摘要；附带空总线与空工具集，失败或空摘要返回 None
    async def _summarize(
        self,
        prompt: str,
        provider: LLMProvider,
    ) -> tuple[str, int] | None:
        try:
            response = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tool_schemas=[],
                bus=EventBus(),
                run_id="compact",
                step=0,
                system=_SUMMARIZER_SYSTEM,
            )
        except Exception:
            logger.exception("compactor: LLM call failed, skipping compaction")
            return None

        summary_text = response.text.strip()
        if not summary_text:
            logger.warning("compactor: LLM returned empty summary, skipping compaction")
            return None
        summary_tokens = (
            response.usage.output_tokens if response.usage else len(summary_text) // 4
        )
        return summary_text, summary_tokens

    # 把摘要写进会话目录的 summary_<时间戳>.md：压缩有损，留痕便于回查
    def _write_summary(self, text: str) -> None:
        if not self._session_id:
            return
        try:
            self._session_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
            (self._session_dir / f"summary_{stamp}.md").write_text(text, encoding="utf-8")
        except OSError:
            logger.exception("compactor: failed to write summary file")
