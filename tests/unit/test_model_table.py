from __future__ import annotations

from weavecode.core.llm import model_table


# 功能：验证在用型号的上下文窗口来自包内模型表，而非散落的硬编码
# 设计：直接断言当前依赖的两个型号，改动表结构时这条会先红
def test_known_model_window_from_table() -> None:
    assert model_table.MODEL_CONTEXT_WINDOWS["claude-sonnet-4-6"] == 1_000_000
    assert model_table.MODEL_CONTEXT_WINDOWS["claude-haiku-4-5"] == 200_000


# 功能：验证表里只登记了在用型号，未收录的 id 不会误命中
# 设计：拿一个不存在的 id 断言缺席，避免后续有人用宽松匹配把兜底变成静默错误
def test_unknown_model_not_registered() -> None:
    assert "no-such-model" not in model_table.MODEL_CONTEXT_WINDOWS
