from __future__ import annotations

# 已知模型的上下文窗口（token）：初版只登记当前在用的几个型号
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-haiku-4-5": 200_000,
}

# 模型不在表里时的兜底窗口：宁可算保守，也不要低估上下文占用
DEFAULT_CONTEXT_WINDOW = 200_000


# 按模型 id 查上下文窗口；未收录时返回兜底值
def context_window(model_id: str) -> int:
    return MODEL_CONTEXT_WINDOWS.get(model_id, DEFAULT_CONTEXT_WINDOW)
