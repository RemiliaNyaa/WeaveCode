from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime
from typing import Any

from weavecode.core.events.bus import EventBus
from weavecode.core.llm.base import LLMProvider
from weavecode.core.llm.types import LlmResponse
from weavecode.core.trace.record import TraceRecord
from weavecode.core.trace.writer import TraceWriter


def _now() -> str:
    return datetime.now(UTC).isoformat()


class TracingProvider:
    # 包裹真实 LLMProvider，在每次 chat() 调用前后向 TraceWriter 写入完整 API I/O 记录
    def __init__(self, inner: LLMProvider, trace: TraceWriter) -> None:
        self._inner = inner
        self._trace = trace

    # 记录 CORE→LLM 请求，调用真实 provider，记录 LLM→CORE 响应（含延迟）
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
        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="CORE→LLM",
                layer="llm",
                kind="api_call",
                data={"messages": messages, "tool_schemas": tool_schemas, "system": system},
            )
        )

        t0 = time.monotonic()
        result = await self._inner.chat(
            messages, tool_schemas, bus, run_id, step=step, system=system
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        self._trace.emit(
            TraceRecord(
                ts=_now(),
                direction="LLM→CORE",
                layer="llm",
                kind="api_response",
                data={
                    "stop_reason": result.stop_reason,
                    "text": result.text,
                    "tool_calls": [dataclasses.asdict(tc) for tc in result.tool_calls],
                    "usage": dataclasses.asdict(result.usage) if result.usage else {},
                    "latency_ms": latency_ms,
                },
            )
        )

        return result
