from __future__ import annotations

from weavecode.core.tools.base import BaseTool


# 工具注册表：dict[名字 → 工具实例]
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    # 注册工具；同名覆盖
    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    # 按名称查找工具，不存在返回 None
    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    # 已注册工具名清单（按注册顺序）
    def names(self) -> list[str]:
        return list(self._tools)

    # 返回所有工具的 Anthropic 格式 schema 列表
    # 每次现场构造一份注册快照：调用方拿到的列表与注册表脱钩，导出与注册互不干扰
    def tool_schemas(self) -> list[dict[str, object]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in self._tools.values()
        ]
