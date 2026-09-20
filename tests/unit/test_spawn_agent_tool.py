from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from weavecode.core.events.bus import EventBus
from weavecode.core.llm.types import LlmResponse, UsageStats
from weavecode.core.subagent.tool import SpawnAgentTool
from weavecode.core.tools.builtin import (
    BashTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
)
from weavecode.core.tools.builtin.update_plan import NoopPlanStorage, UpdatePlanTool


def _make_provider(result_text: str = "child done") -> Any:
    provider = AsyncMock()
    provider.chat = AsyncMock(
        return_value=LlmResponse(
            stop_reason="end_turn",
            tool_calls=[],
            text=result_text,
            usage=UsageStats(
                input_tokens=10,
                output_tokens=5,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
                context_pct=0.01,
            ),
        )
    )
    return provider


# 子 Agent 的内置工具清单：与主 Agent 同一份定义，只是计划不落盘
def _child_tools() -> list[Any]:
    return [
        ReadFileTool(), BashTool(), WriteFileTool(), ListDirTool(),
        GlobTool(), GrepTool(), UpdatePlanTool(NoopPlanStorage()),
    ]


def _make_tool(
    tmp_path: Path,
    provider: Any = None,
) -> tuple[SpawnAgentTool, EventBus]:
    bus = EventBus()
    tool = SpawnAgentTool(
        provider=provider or _make_provider(),
        parent_bus=bus,
        parent_run_id="parent-run-01",
        permission_manager=None,
        max_steps=5,
        session_id="sess-test",
        child_tools=_child_tools,
    )
    return tool, bus


# 功能：spawn_agent 应阻塞直到子 agent 完成并返回其结果
# 设计：使用返回 end_turn 的 mock provider，验证 tool_result.content 包含 provider 返回的文字
@pytest.mark.asyncio
async def test_foreground_returns_result(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path, _make_provider("analysis complete"))
    result = await tool.invoke({
        "description": "分析代码",
        "prompt": "分析 src/ 目录",
    })
    assert not result.is_error
    assert "analysis complete" in result.content


# 功能：验证子 Agent 拿到主 Agent 的全部内置工具，但**不含 spawn_agent**（防嵌套）
# 设计：直接构造子 registry 用 get() 断言各工具在/不在，锁住「同主 Agent 减去派生工具」这条不变式
def test_child_registry_excludes_spawn_agent(tmp_path: Path) -> None:
    tool, _ = _make_tool(tmp_path)
    registry = tool._build_child_registry()

    for name in ("read_file", "bash", "write_file", "list_dir", "glob", "grep", "update_plan"):
        assert registry.get(name) is not None, f"子 Agent 缺少内置工具 {name}"
    assert registry.get("spawn_agent") is None


# 功能：验证传入的 MCP 工具会被一并注册进子 Agent
# 设计：用一个最小 BaseTool 冒充 MCP 工具，断言子 registry 能拿到（子 Agent 工具集 = 主 Agent 工具集）
def test_child_registry_includes_mcp_tools(tmp_path: Path) -> None:
    from weavecode.core.tools.base import BaseTool, ToolResult

    class _FakeMcpTool(BaseTool):
        name = "filesystem__read_file"
        description = "fake mcp tool"
        input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

        async def invoke(self, params: dict[str, object]) -> ToolResult:
            return ToolResult(content="ok")

    bus = EventBus()
    tool = SpawnAgentTool(
        provider=_make_provider(),
        parent_bus=bus,
        parent_run_id="parent-run-01",
        permission_manager=None,
        max_steps=5,
        session_id="sess-test",
        child_tools=_child_tools,
        mcp_tools=[_FakeMcpTool()],
    )

    registry = tool._build_child_registry()

    assert registry.get("filesystem__read_file") is not None
