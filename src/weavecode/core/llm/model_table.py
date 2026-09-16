from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).parent.parent.parent / "data" / "models.json"

# 模型不在表中时的兜底值（宁可保守：压缩更早触发，也不会真的溢出）
DEFAULT_CONTEXT_WINDOW = 200_000
DEFAULT_MAX_OUTPUT = 8_192


@dataclass(frozen=True)
class ModelLimits:
    id: str
    name: str
    context: int
    output: int
    reasoning: bool


# 读取包内模型表，按模型 id 扁平化为 {id: ModelLimits}
def _load() -> dict[str, ModelLimits]:
    try:
        raw: Any = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("model table unavailable at %s, falling back to defaults", _DATA_PATH)
        return {}

    table: dict[str, ModelLimits] = {}
    for provider in (raw.get("providers") or {}).values():
        for model_id, entry in (provider.get("models") or {}).items():
            limits = ModelLimits(
                id=model_id,
                name=str(entry.get("name") or model_id),
                context=int(entry.get("context") or 0),
                output=int(entry.get("output") or 0),
                reasoning=bool(entry.get("reasoning")),
            )
            existing = table.get(model_id)
            if existing is None:
                table[model_id] = limits
            else:
                # 同一模型 id 出现在多个厂商且数值不一致时，取更保守的一组
                table[model_id] = ModelLimits(
                    id=model_id,
                    name=existing.name,
                    context=_conservative(existing.context, limits.context),
                    output=_conservative(existing.output, limits.output),
                    reasoning=existing.reasoning,
                )
    return table


# 取两个上限里更保守的一个；0 视为未知，两者都未知时返回 0
def _conservative(a: int, b: int) -> int:
    valid = [value for value in (a, b) if value > 0]
    return min(valid) if valid else 0


# 返回缓存后的模型表
@lru_cache(maxsize=1)
def table() -> dict[str, ModelLimits]:
    return _load()


# 按模型 id 查上限；未收录时返回 None
def lookup(model_id: str) -> ModelLimits | None:
    limits = table().get(model_id)
    if limits is not None:
        return limits
    lowered = model_id.strip().lower()
    for key, value in table().items():
        if key.lower() == lowered:
            return value
    return None


# 返回模型的上下文窗口；未收录时用兜底值
def context_window(model_id: str) -> int:
    limits = lookup(model_id)
    if limits is None or limits.context <= 0:
        return DEFAULT_CONTEXT_WINDOW
    return limits.context


# 返回模型的最大输出 token；未收录或未知时用兜底值
def max_output(model_id: str) -> int:
    limits = lookup(model_id)
    if limits is None or limits.output <= 0:
        return DEFAULT_MAX_OUTPUT
    return limits.output


# 返回模型的展示名；未收录时原样返回 id
def display_name(model_id: str) -> str:
    limits = lookup(model_id)
    return limits.name if limits is not None else model_id
