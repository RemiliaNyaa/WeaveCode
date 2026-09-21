from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import BaseModel

from weavecode.core.events.bus import EventBus
from weavecode.core.llm.provider import AnthropicProvider
from weavecode.core.llm.types import LlmResponse

# --- helpers -----------------------------------------------------------------


def _make_usage(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_create: int = 0,
) -> MagicMock:
    u = MagicMock()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_input_tokens = cache_read
    u.cache_creation_input_tokens = cache_create
    return u


def _make_final(
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> MagicMock:
    msg = MagicMock()
    msg.stop_reason = stop_reason
    msg.content = content or []
    msg.usage = _make_usage(input_tokens, output_tokens, cache_read)
    return msg


class FakeStream:
    """Minimal async context manager that fakes the anthropic streaming interface."""

    def __init__(self, texts: list[str], final: MagicMock) -> None:
        self._texts = texts
        self._final = final

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    @property
    def text_stream(self):  # type: ignore[return]
        async def _gen():
            for t in self._texts:
                yield t

        return _gen()

    async def get_final_message(self) -> MagicMock:
        return self._final


def _make_provider(
    texts: list[str] | None = None,
    stop_reason: str = "end_turn",
    content: list[MagicMock] | None = None,
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 0,
) -> tuple[AnthropicProvider, MagicMock]:
    final = _make_final(stop_reason, content, input_tokens, output_tokens, cache_read)
    client = MagicMock()
    client.messages.stream.return_value = FakeStream(texts or [], final)
    return AnthropicProvider(model="test-model", client=client), client


async def _chat(
    provider: AnthropicProvider,
    messages: list[dict[str, object]] | None = None,
    tool_schemas: list[dict[str, object]] | None = None,
) -> tuple[LlmResponse, list[BaseModel]]:
    collected: list[BaseModel] = []
    bus = EventBus()

    async def _collect(e: BaseModel) -> None:
        collected.append(e)

    bus.subscribe(_collect)
    result = await provider.chat(
        messages=messages or [],
        tool_schemas=tool_schemas or [],
        bus=bus,
        run_id="r1",
    )
    return result, collected


# --- tests -------------------------------------------------------------------


# 功能：验证每次 chat 调用都发布携带模型名和路由策略的 llm.model_selected 事件
# 设计：检查事件数量为 1 且字段精确匹配，因为 S6 路由报告依赖此事件统计模型使用分布
async def test_model_selected_event_published() -> None:
    provider, _ = _make_provider()
    _, events = await _chat(provider)
    sel = [e for e in events if e.type == "llm.model_selected"]  # type: ignore[attr-defined]
    assert len(sel) == 1
    assert sel[0].model == "test-model"  # type: ignore[attr-defined]
    assert sel[0].strategy == "static"  # type: ignore[attr-defined]
    assert sel[0].run_id == "r1"  # type: ignore[attr-defined]


# 功能：验证流式响应的每个 token 触发独立的 llm.token 事件，内容与顺序均正确
# 设计：使用 FakeStream 控制精确的 token 序列，断言数量和各 token 的值，排除批量合并或跳过情况
async def test_token_events_published_per_chunk() -> None:
    provider, _ = _make_provider(texts=["Hello", " world"])
    _, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert len(tokens) == 2
    assert tokens[0].token == "Hello"  # type: ignore[attr-defined]
    assert tokens[1].token == " world"  # type: ignore[attr-defined]


# 功能：验证 llm.usage 事件中的 token 统计字段（input、output、cache_read）正确
# 设计：向 FakeStream 注入固定 usage 值，断言三个字段精确匹配，因为这些字段是 S6 成本计算的数据源
async def test_usage_event_published_after_stream() -> None:
    provider, _ = _make_provider(input_tokens=200, output_tokens=75, cache_read=150)
    _, events = await _chat(provider)
    usage_events = [e for e in events if e.type == "llm.usage"]  # type: ignore[attr-defined]
    assert len(usage_events) == 1
    ue = usage_events[0]
    assert ue.input_tokens == 200  # type: ignore[attr-defined]
    assert ue.output_tokens == 75  # type: ignore[attr-defined]
    assert ue.cache_read_input_tokens == 150  # type: ignore[attr-defined]


# 功能：验证事件发布顺序为 model_selected → token（×N） → usage
# 设计：检查类型列表的首尾元素，聚焦 provider 的时序契约，而非中间 token 事件的顺序（那由流本身决定）
async def test_event_order_model_selected_first_usage_last() -> None:
    provider, _ = _make_provider(texts=["hi"])
    _, events = await _chat(provider)
    types = [e.type for e in events]  # type: ignore[attr-defined]
    assert types[0] == "llm.model_selected"
    assert "llm.token" in types
    assert types[-1] == "llm.usage"


# 功能：验证 stop_reason=tool_use 时 final_message content block 被正确解析为 ToolCallBlock
# 设计：注入带 tool_use block 的 final_message，逐字段检查 ToolCallBlock 的 id/name/input，确认解析路径完整
async def test_tool_use_parsed_from_final_message() -> None:
    tool_block = MagicMock()
    tool_block.type = "tool_use"
    tool_block.id = "toolu_01"
    tool_block.name = "read_file"
    tool_block.input = {"path": "README.md"}
    provider, _ = _make_provider(stop_reason="tool_use", content=[tool_block])
    result, _ = await _chat(provider)
    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.id == "toolu_01"
    assert tc.name == "read_file"
    assert tc.input == {"path": "README.md"}


# 功能：验证 stop_reason=end_turn 时不产生任何工具调用
# 设计：与 test_tool_use_parsed 互补，确认 stop_reason 决策树的另一侧，防止 end_turn 响应被误解析为 tool_use
async def test_end_turn_produces_no_tool_calls() -> None:
    provider, _ = _make_provider(stop_reason="end_turn")
    result, _ = await _chat(provider)
    assert result.stop_reason == "end_turn"
    assert result.tool_calls == []


# 功能：验证多个流式 token 被正确拼接为 LlmResponse.text 字段
# 设计：三段 token 拼接，检查 result.text 完整字符串，因为 StdoutPrinter 消费 text 字段展示最终输出
async def test_text_accumulated_from_tokens() -> None:
    provider, _ = _make_provider(texts=["foo", "bar", "baz"])
    result, _ = await _chat(provider)
    assert result.text == "foobarbaz"


# 功能：验证缺少 ANTHROPIC_API_KEY 时 provider 初始化立即 SystemExit 而非等到调用时才报错
# 设计：用 monkeypatch 清除环境变量后实例化，确认 fail-fast 行为，防止"幽灵 run"（有 started 但无 finished 事件）
async def test_missing_api_key_raises_system_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        AnthropicProvider(model="any")


# 功能：验证空流式响应不发布任何 llm.token 事件且 result.text 为空字符串
# 设计：texts=[] 覆盖零 token 边界条件，确认 text="" 而非 None，避免调用方对空内容做额外 None 判断
async def test_no_tokens_when_response_is_empty() -> None:
    provider, _ = _make_provider(texts=[])
    result, events = await _chat(provider)
    tokens = [e for e in events if e.type == "llm.token"]  # type: ignore[attr-defined]
    assert tokens == []
    assert result.text == ""


# ---------------------------- 流式重试（改动三十二） ----------------------------


class _RecordingSleep:
    """假的 asyncio.sleep：记录退避时长、不真的等。"""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class FailingStream:
    """先吐几个 token、再抛传输异常——模拟「流到一半断了」。"""

    def __init__(self, texts: list[str], exc: Exception) -> None:
        self._texts = texts
        self._exc = exc

    async def __aenter__(self) -> FailingStream:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    @property
    def text_stream(self):  # type: ignore[return]
        async def _gen():
            for t in self._texts:
                yield t
            raise self._exc

        return _gen()

    async def get_final_message(self) -> MagicMock:
        raise AssertionError("流已经断了，不该走到 get_final_message")


# 造一个「前几次抛指定异常、之后成功」的假 provider
def _flaky_provider(
    failures: list[Exception], texts: list[str], max_retries: int = 5
) -> tuple[AnthropicProvider, MagicMock]:
    final = _make_final("end_turn", None, 100, 50, 0)
    client = MagicMock()
    client.messages.stream.side_effect = [*failures, FakeStream(texts, final)]
    return AnthropicProvider(model="test-model", client=client, max_retries=max_retries), client


# 功能：流式调用遇到传输类异常时会重试，重试成功后正常返回结果
# 设计：第一次抛 httpx.ReadError、第二次成功；断言拿到结果且 stream 被调了 2 次
@pytest.mark.asyncio
async def test_stream_retry_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _RecordingSleep())
    provider, client = _flaky_provider([httpx.ReadError("dropped")], ["hello"])

    resp, _ = await _chat(provider)

    assert resp.text == "hello"
    assert client.messages.stream.call_count == 2


# 功能：非传输类异常不重试，直接抛出去
# 设计：抛 ValueError 并断言 stream 只被调一次——参数错/鉴权失败这类重试也没用，不该浪费尝试
@pytest.mark.asyncio
async def test_non_transport_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _RecordingSleep())
    provider, client = _flaky_provider([ValueError("bad request")], ["hello"])

    with pytest.raises(ValueError):
        await _chat(provider)

    assert client.messages.stream.call_count == 1


# 功能：重试时丢弃上一次已收到的 partial token，不把两次输出拼起来
# 设计：第一次吐 "AAA" 后断、第二次吐 "BBB"；断言最终 text 是 "BBB" 而不是 "AAABBB"——
#       这是 text_parts 每次尝试开头被重置的证据，否则输出会变成重复的乱码
@pytest.mark.asyncio
async def test_partial_output_discarded_on_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _RecordingSleep())
    final = _make_final("end_turn", None, 100, 50, 0)
    client = MagicMock()
    client.messages.stream.side_effect = [
        FailingStream(["AAA"], httpx.ReadError("dropped")),
        FakeStream(["BBB"], final),
    ]
    provider = AnthropicProvider(model="test-model", client=client)

    resp, _ = await _chat(provider)

    assert resp.text == "BBB"


# 功能：只在第 1 次尝试发 llm.token 事件，重试不再重复发
# 设计：第一次吐 "AAA" 后断、第二次吐 "BBB"；断言 token 事件只有 "AAA"——
#       否则 TUI 上同一段话会被渲染两遍
@pytest.mark.asyncio
async def test_token_events_only_from_first_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _RecordingSleep())
    final = _make_final("end_turn", None, 100, 50, 0)
    client = MagicMock()
    client.messages.stream.side_effect = [
        FailingStream(["AAA"], httpx.ReadError("dropped")),
        FakeStream(["BBB"], final),
    ]
    provider = AnthropicProvider(model="test-model", client=client)

    _, events = await _chat(provider)

    tokens = [e.token for e in events if e.__class__.__name__ == "LlmTokenEvent"]
    assert tokens == ["AAA"]


# 功能：用完重试次数后抛出异常，总尝试次数 = 1 次首发 + max_retries 次重试
# 设计：max_retries=5、连续 6 次失败；断言 stream 恰好被调 6 次且异常抛出——
#       锁住「重试次数」的口径（是重试次数，不是尝试次数）
@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asyncio, "sleep", _RecordingSleep())
    provider, client = _flaky_provider([httpx.ReadError("x")] * 6, ["hello"], max_retries=5)

    with pytest.raises(httpx.ReadError):
        await _chat(provider)

    assert client.messages.stream.call_count == 6


# 功能：退避时长现场计算（2/4/8/16/32 秒），不写死元组
# 设计：记录每次 sleep 的时长并断言精确序列——这条同时保证「重试次数改了退避自动跟着变」
@pytest.mark.asyncio
async def test_backoff_is_computed_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = _RecordingSleep()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    provider, _ = _flaky_provider([httpx.ReadError("x")] * 6, ["hello"], max_retries=5)

    with pytest.raises(httpx.ReadError):
        await _chat(provider)

    assert sleep.delays == [2.0, 4.0, 8.0, 16.0, 32.0]
