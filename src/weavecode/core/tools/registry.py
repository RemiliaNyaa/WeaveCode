from __future__ import annotations

from weavecode.core.tools.base import BaseTool


# 工具注册表：dict[名字 → 工具实例]
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        # 已注册工具的 schema 列表缓存：注册时重建，导出时直接交出本体
        self._schemas: list[dict[str, object]] = []

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        self._schemas = [
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
            }
            for item in self._tools.values()
        ]

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 已注册工具名清单（按注册顺序）
    def names(self) -> list[str]:
        return list(self._tools)

    # 返回所有工具的 Anthropic 格式 schema 列表
    def tool_schemas(self) -> list[dict[str, object]]:
        return self._schemas
