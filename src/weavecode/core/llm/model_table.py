from __future__ import annotations

# 已知模型的上下文窗口（token）：初版只登记当前在用的几个型号
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-sonnet-4-6": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-haiku-4-5": 200_000,
}
