from __future__ import annotations

from typing import Any

from weavecode.core.compact.compactor import CompactionResult
from weavecode.core.context import ExecutionContext
from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, UsageStats
from weavecode.core.loop import AgentLoop
from weavecode.core.tools.registry import ToolRegistry


class _StubProvider:
    """按顺序返回预设响应，并记录每次请求的消息条数。"""

    def __init__(self, responses: list[LlmResponse]) -> None:
        self._responses = iter(responses)
        self.seen_message_counts: list[int] = []

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
        self.seen_message_counts.append(len(messages))
        return next(self._responses)


class _StubCompactor:
    """记录 compact 调用次数；result 为 None 时模拟压缩失败。"""

    def __init__(self, result: CompactionResult | None) -> None:
        self.calls = 0
        self._result = result

    async def compact(
        self,
        context: ExecutionContext,
        provider: Any,
        focus: str = "",
    ) -> CompactionResult | None:
        self.calls += 1
        if self._result is None:
            return None
        context.messages = self._result.as_messages()
        return self._result


_RESULT = CompactionResult(
    summary_text="## 1. Original Goal\nGoal",
    original_token_estimate=1,
    summary_tokens=10,
)


# 返回一轮就结束的响应，携带 usage 供校准逻辑使用
def _done(text: str = "done") -> LlmResponse:
    return LlmResponse(
        stop_reason="end_turn",
        text=text,
        usage=UsageStats(input_tokens=10, output_tokens=5),
    )


# 功能：验证输入装得下时不触发压缩
# 设计：首轮响应的上下文水位远低于阈值，断言 compactor 一次都没被调用
async def test_no_compaction_when_input_fits() -> None:
    provider = _StubProvider([_done()])
    compactor = _StubCompactor(_RESULT)
    loop = AgentLoop(provider, ToolRegistry(), EventBus(), compactor=compactor)  # type: ignore[arg-type]
    context = ExecutionContext(run_id="r1", goal="hi", max_steps=3)

    await loop.run(context)

    assert compactor.calls == 0


# 功能：验证没有注入压缩器时不会报错（测试环境常见）
# 设计：compactor 缺省，超长目标仍应正常跑完
async def test_missing_compactor_is_noop() -> None:
    provider = _StubProvider([_done()])
    loop = AgentLoop(provider, ToolRegistry(), EventBus())  # type: ignore[arg-type]
    context = ExecutionContext(run_id="r1", goal="x" * 20_000, max_steps=3)

    await loop.run(context)

    assert context.status == "success"
