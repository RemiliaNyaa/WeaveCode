from __future__ import annotations

import asyncio
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from weavecode.core.skills.loader import SkillLoader


@dataclass
class ExecutionContext:
    run_id: str
    goal: str
    max_steps: int
    prefill_messages: list[dict[str, Any]] = field(default_factory=list)
    global_context: str = ""
    project_context: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    status: str = "running"  # "running" | "success" | "failed"
    reason: str | None = None
    result: str = ""
    # 本会话工作目录（客户端启动目录的绝对路径）；空串回退到进程 cwd
    working_dir: str = ""

    # 返回本次运行实际生效的工作目录绝对路径
    def effective_working_dir(self) -> str:
        return self.working_dir or str(Path.cwd())

    # 初始化消息历史，优先使用 session 完整回放内容
    def __post_init__(self) -> None:
        if self.prefill_messages:
            self.messages = [dict(m) for m in self.prefill_messages]
        elif not self.messages:
            self.messages.append({"role": "user", "content": self.goal})

    # 返回当前 run 的 system prompt：base + env + 规则层 + skill 清单
    # skill_catalog 可由调用方预先渲染（扫盘是阻塞 IO，生产路径由 loop 放进线程）；None 时现场渲染
    def system_prompt(self, base: str, *, skill_catalog: str | None = None) -> str:
        parts = [base]
        # 注入运行环境信息（对齐 OpenCode 的 <env> 块，反映会话工作目录）
        parts.append(
            "\n\n<env>\n"
            f"  Working directory: {self.effective_working_dir()}\n"
            f"  Platform: {platform.system()}\n"
            "</env>"
        )
        has_global = bool(self.global_context.strip())
        has_project = bool(self.project_context.strip())
        if has_global:
            parts.append("\n\n## Global Rules (AGENTS.md)\n" + self.global_context.strip())
        if has_project:
            parts.append("\n\n## Project Rules (AGENTS.md)\n" + self.project_context.strip())
        # 全局与项目同时存在时才声明冲突裁决规则（只有一份时无从冲突）
        if has_global and has_project:
            parts.append(
                "\n\nIf a project rule conflicts with a global rule, the project rule wins."
            )
        # skill 清单每次现场渲染（按会话工作目录解析项目本地 skill），中途改 skill 下一步即生效
        if skill_catalog is None:
            skill_catalog = SkillLoader(self.effective_working_dir()).render_catalog()
        parts.append("\n\n## Skills\n" + skill_catalog)
        return "".join(parts)

    # 异步版 system prompt：skill 清单要扫盘（阻塞 IO），放进线程渲染后再拼装，不卡事件循环
    async def system_prompt_async(self, base: str) -> str:
        catalog = await asyncio.to_thread(
            SkillLoader(self.effective_working_dir()).render_catalog
        )
        return self.system_prompt(base, skill_catalog=catalog)

    # 将 LLM 响应的 content blocks 追加为 assistant 消息
    def add_assistant_message(self, content: list[Any]) -> None:
        self.messages.append({"role": "assistant", "content": content})

    # 将工具调用结果追加为 user 消息；同一步的多个结果共享同一条消息
    def add_tool_result(
        self, tool_use_id: str, content: str, is_error: bool = False
    ) -> None:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": content,
        }
        if is_error:
            block["is_error"] = True

        last = self.messages[-1] if self.messages else None
        if (
            last is not None
            and last["role"] == "user"
            and isinstance(last["content"], list)
            and last["content"]
            and all(b.get("type") == "tool_result" for b in last["content"])
        ):
            last["content"].append(block)
        else:
            self.messages.append({"role": "user", "content": [block]})

    # 追加一条 user 消息（loop 侧收工兜底用：提醒模型 / 系统代收结果）
    # 刻意不改 system prompt —— system+tools 是 prompt caching 的前缀，改了会让缓存整体失效
    def add_user_notice(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    # 返回 True 表示 loop 应停止（状态不再是 running）
    def is_done(self) -> bool:
        return self.status != "running"

    # 将 run 标记为成功
    def mark_success(self) -> None:
        self.status = "success"

    # 将 run 标记为失败并记录原因
    def mark_failed(self, reason: str) -> None:
        self.status = "failed"
        self.reason = reason
