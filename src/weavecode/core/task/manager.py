from __future__ import annotations

import json
from pathlib import Path

from weavecode.core.task.model import Task, _now


# 任务存储：每个任务一个 task_{id}.json
#
# 目录由调用方按「会话目录」传入，实例由 SessionManager 按 session 缓存复用——
# 运行期间读内存副本（快），每次写都落盘（崩溃/重启不丢），id 跨 run 连续不再归零。
class TaskManager:
    def __init__(self, tasks_dir: Path) -> None:
        self._dir = Path(tasks_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[int, Task] = {}
        self._loaded = False
        self._next_id = self._max_id() + 1

    # 新建任务：写进内存副本并落盘，然后自增编号
    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
    ) -> Task:
        task = Task(
            id=self._next_id,
            subject=subject,
            description=description,
            blocked_by=list(blocked_by or []),
        )
        self._next_id += 1
        self._tasks[task.id] = task
        self._save(task)
        return task

    # 按 id 取单个任务，没有则返回 None
    def get(self, task_id: int) -> Task | None:
        self._ensure_loaded()
        return self._tasks.get(task_id)

    # 列出全部任务（按 id 升序），可按状态过滤
    def list(self, status: str | None = None) -> list[Task]:
        self._ensure_loaded()
        tasks = [task for task in self._tasks.values() if status is None or task.status == status]
        tasks.sort(key=lambda item: item.id)
        return tasks

    # 更新任务的状态 / 描述 / 前置依赖；标完成时自动解除其他任务的阻塞
    def update(
        self,
        task_id: int,
        *,
        status: str | None = None,
        description: str | None = None,
        add_blocked_by: list[int] | None = None,
        remove_blocked_by: list[int] | None = None,
    ) -> Task:
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")

        if status is not None:
            task.status = status
        if description is not None:
            task.description = description
        if add_blocked_by:
            for dep in add_blocked_by:
                if dep != task_id and dep not in task.blocked_by:
                    task.blocked_by.append(dep)
        if remove_blocked_by:
            task.blocked_by = [x for x in task.blocked_by if x not in remove_blocked_by]
        task.updated_at = _now()

        self._save(task)
        if task.status == "completed":
            self._clear_dependency(task_id)
        return task

    # 前置依赖查询：这个任务还在等哪些任务完成
    def blocked_by(self, task_id: int) -> list[Task]:
        self._ensure_loaded()
        task = self._tasks.get(task_id)
        if task is None:
            return []
        deps: list[Task] = []
        for dep_id in task.blocked_by:
            dep = self._tasks.get(dep_id)
            if dep is not None:
                deps.append(dep)
        return deps

    # 首次访问时把目录里的任务文件读进内存，之后一律读内存副本
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        for path in self._dir.glob("task_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                task = Task.from_dict(data)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            self._tasks[task.id] = task
        self._loaded = True
        if self._next_id <= max(self._tasks, default=0):
            self._next_id = max(self._tasks) + 1

    # 单个任务的文件路径
    def _path(self, task_id: int) -> Path:
        return self._dir / f"task_{task_id}.json"

    # 写回任务文件（内存副本同步更新）
    def _save(self, task: Task) -> None:
        self._path(task.id).write_text(
            json.dumps(task.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # 扫描已有文件取最大编号，供自增起点使用
    def _max_id(self) -> int:
        highest = 0
        for path in self._dir.glob("task_*.json"):
            task_id = self._parse_id(path)
            if task_id > highest:
                highest = task_id
        return highest

    # 文件名 task_{id}.json → id；不认识的文件按 0 处理
    def _parse_id(self, path: Path) -> int:
        try:
            return int(path.stem.removeprefix("task_"))
        except ValueError:
            return 0

    # 任务完成时解除依赖：把 blocked_by 里含该 id 的条目全部移除，内存与文件一起改
    def _clear_dependency(self, completed_id: int) -> None:
        for task in list(self._tasks.values()):
            if completed_id not in task.blocked_by:
                continue
            task.blocked_by = [x for x in task.blocked_by if x != completed_id]
            task.updated_at = _now()
            self._save(task)
