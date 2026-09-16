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
    recent_text="[USER]\nrecent",
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
# 设计：reserve 设得很小，预算充裕，断言 compactor 一次都没被调用
async def test_no_compaction_when_input_fits() -> None:
    provider = _StubProvider([_done()])
    compactor = _StubCompactor(_RESULT)
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        compactor=compactor, reserve_tokens=1_000, model="",
    )
    context = ExecutionContext(run_id="r1", goal="hi", max_steps=3)

    await loop.run(context)

    assert compactor.calls == 0


# 功能：验证发请求前预判超限时会先压缩，再发请求
# 设计：reserve 逼近窗口使预算极小，20K 字符的历史必然超限；断言压缩一次
async def test_compaction_runs_before_request() -> None:
    provider = _StubProvider([_done()])
    compactor = _StubCompactor(_RESULT)
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        compactor=compactor, reserve_tokens=199_000, model="",
    )
    context = ExecutionContext(
        run_id="r1", goal="g", max_steps=3,
        prefill_messages=[{"role": "user", "content": "x" * 20_000}],
    )

    await loop.run(context)

    assert compactor.calls == 1
    # 压缩结果位于历史开头，说明压缩发生在请求之前
    assert str(context.messages[0]["content"]).startswith("<conversation-checkpoint>")


# 功能：验证压缩失败时走确定性降级，把历史丢到预算以内且 run 仍继续
# 设计：stub 压缩器返回 None，30 条历史应被裁剪；断言调用发生、历史变短、run 成功
async def test_compaction_failure_falls_back_to_shrink() -> None:
    provider = _StubProvider([_done()])
    compactor = _StubCompactor(None)
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        compactor=compactor, reserve_tokens=199_000, model="",
    )
    context = ExecutionContext(
        run_id="r1", goal="g", max_steps=3,
        prefill_messages=[{"role": "user", "content": "x" * 400} for _ in range(30)],
    )

    await loop.run(context)

    assert compactor.calls == 1
    assert len(context.messages) < 31
    assert context.status == "success"


# 功能：验证 auto_compact 关闭时完全不做压缩
# 设计：同样的超限输入，仅关掉开关，断言 compactor 未被调用
async def test_auto_compact_disabled_skips_compaction() -> None:
    provider = _StubProvider([_done()])
    compactor = _StubCompactor(_RESULT)
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        compactor=compactor, auto_compact=False, reserve_tokens=199_000, model="",
    )
    context = ExecutionContext(
        run_id="r1", goal="g", max_steps=3,
        prefill_messages=[{"role": "user", "content": "x" * 20_000}],
    )

    await loop.run(context)

    assert compactor.calls == 0


# 功能：验证没有注入压缩器时不会报错（测试环境常见）
# 设计：compactor 缺省，超限输入仍应正常跑完
async def test_missing_compactor_is_noop() -> None:
    provider = _StubProvider([_done()])
    loop = AgentLoop(
        provider, ToolRegistry(), EventBus(),  # type: ignore[arg-type]
        reserve_tokens=1, model="",
    )
    context = ExecutionContext(run_id="r1", goal="x" * 20_000, max_steps=3)

    await loop.run(context)

    assert context.status == "success"
