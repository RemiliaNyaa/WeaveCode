from __future__ import annotations

from weavecode.core.llm import model_table


# 功能：验证已知模型的上下文窗口来自包内模型表，而非硬编码的 200K
# 设计：直接断言 DeepSeek V4 Flash 的 1M 窗口，锁死「窗口有数据源」这一行为
def test_context_window_from_table() -> None:
    assert model_table.context_window("deepseek-v4-flash") == 1_000_000


# 功能：验证未收录的模型退回兜底窗口，不会抛异常
# 设计：用一个不存在的 id，断言等于 DEFAULT_CONTEXT_WINDOW
def test_context_window_unknown_falls_back() -> None:
    assert model_table.context_window("no-such-model") == model_table.DEFAULT_CONTEXT_WINDOW


# 功能：验证输出上限查询与兜底
# 设计：已知模型取表内值，未知模型取 DEFAULT_MAX_OUTPUT
def test_max_output_and_fallback() -> None:
    assert model_table.max_output("deepseek-v4-flash") == 384_000
    assert model_table.max_output("no-such-model") == model_table.DEFAULT_MAX_OUTPUT


# 功能：验证同一模型 id 在多厂商数值不一致时取更保守（更小）的一组
# 设计：siliconflow 记 1000000/384000、siliconflow-cn 记 1049000/393000，断言结果为前者
def test_conflicting_entries_are_conservative() -> None:
    assert model_table.context_window("deepseek-ai/DeepSeek-V4-Pro") == 1_000_000


# 功能：验证模型 id 查询大小写不敏感
# 设计：用首字母大写的形式查小写 id，断言能命中
def test_lookup_is_case_insensitive() -> None:
    limits = model_table.lookup("DeepSeek-V4-Flash")
    assert limits is not None
    assert limits.id == "deepseek-v4-flash"


# 功能：验证展示名来自模型表，未收录时原样返回 id
# 设计：分别覆盖命中与未命中两条分支
def test_display_name() -> None:
    assert model_table.display_name("deepseek-v4-flash") == "DeepSeek V4 Flash"
    assert model_table.display_name("no-such-model") == "no-such-model"


# 功能：验证收录的都是可用的 agent 模型（有非零窗口、上限为正）
# 设计：全表扫描，保证生成脚本的筛选条件（tool_call / context > 0）没有被绕过
def test_every_entry_has_positive_limits() -> None:
    entries = model_table.table()
    assert entries
    for model_id, limits in entries.items():
        assert limits.context > 0, model_id
        assert limits.output > 0, model_id
