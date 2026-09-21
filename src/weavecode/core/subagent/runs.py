from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class SubagentOutcome:
    # 后台子 Agent 的最终结果：run_id、状态（success/failed）、文本输出
    run_id: str
    status: str
    result: str


# 后台子 Agent 的登记表：派出去时先记账，wait_agent 再凭 run_id 收账
class BackgroundRuns:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._outcomes: dict[str, SubagentOutcome] = {}

    # 登记一个后台子 Agent 的运行任务（run_id 由派生方生成）
    def add(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[run_id] = task

    # 记录某个后台子 Agent 的最终结果
    def finish(self, run_id: str, outcome: SubagentOutcome) -> None:
        self._outcomes[run_id] = outcome

    # 尚未拿到结果的 run_id 列表（按登记顺序，供收工兜底判断"还有没有在跑的"）
    def pending_ids(self) -> list[str]:
        return [rid for rid in self._tasks if rid not in self._outcomes]

    # 已登记的 run_id 列表（按登记顺序）
    def known_ids(self) -> list[str]:
        return list(self._tasks)

    # 返回某个 run 的结果；尚未完成时返回 None
    def outcome(self, run_id: str) -> SubagentOutcome | None:
        return self._outcomes.get(run_id)

    # 阻塞等待指定 run 出结果，返回各自结果
    #
    # run_ids 为 None/空时针对**全部已登记**的 run（不只是还在跑的）——否则
    # 「先跑完、后收账」的结果会被漏掉：模型干完自己的活再来 wait 时，pending 已经空了。
    async def wait(self, run_ids: list[str] | None = None) -> list[SubagentOutcome]:
        targets = list(run_ids) if run_ids else self.known_ids()
        tasks = [self._tasks[rid] for rid in targets if rid in self._tasks]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return [self._outcomes[rid] for rid in targets if rid in self._outcomes]

    # 取消所有仍在运行的后台子 Agent（run 收尾清理：用户中止 / LLM 错误 / 超步数）
    async def cancel_all(self) -> None:
        pending = self.pending_ids()
        tasks = [self._tasks[rid] for rid in pending if rid in self._tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # 兜底：子任务没来得及自己记结果时补一条 cancelled，避免 pending 残留
        for rid in pending:
            if rid not in self._outcomes:
                self.finish(rid, SubagentOutcome(rid, "failed", "cancelled"))
