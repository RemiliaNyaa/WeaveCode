from __future__ import annotations

import json
from pathlib import Path

from weavecode.core.task.model import Task, _now


# 任务存储：每个任务一个 task_{id}.json，纯同步的文件 CRUD 层
class TaskManager:
    def __init__(self, tasks_dir: Path) -> None:
        self._dir = Path(tasks_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1

    # 新建任务：写盘后自增编号
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
        self._save(task)
        return task

    # 按 id 取单个任务，文件不存在返回 None
    def get(self, task_id: int) -> Task | None:
        return self._load(task_id)

    # 列出全部任务（按 id 升序），可按状态过滤
    def list(self, status: str | None = None) -> list[Task]:
        tasks: list[Task] = []
        for path in self._dir.glob("task_*.json"):
            task = self._load(self._parse_id(path))
            if task is None:
                continue
            if status is not None and task.status != status:
                continue
            tasks.append(task)
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
        task = self._load(task_id)
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
        task = self._load(task_id)
        if task is None:
            return []
        deps: list[Task] = []
        for dep_id in task.blocked_by:
            dep = self._load(dep_id)
            if dep is not None:
                deps.append(dep)
        return deps

    # 单个任务的文件路径
    def _path(self, task_id: int) -> Path:
        return self._dir / f"task_{task_id}.json"

    # 读一个任务文件
    def _load(self, task_id: int) -> Task | None:
        path = self._path(task_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return Task(
            id=int(data["id"]),
            subject=str(data["subject"]),
            description=str(data.get("description", "")),
            status=str(data.get("status", "pending")),
            blocked_by=[int(x) for x in (data.get("blocked_by") or [])],
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )

    # 写回任务文件
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

    # 任务完成时解除依赖：把 blocked_by 里含该 id 的条目全部移除
    def _clear_dependency(self, completed_id: int) -> None:
        for path in self._dir.glob("task_*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            blocked = [int(x) for x in data.get("blocked_by", [])]
            if completed_id in blocked:
                data["blocked_by"] = [x for x in blocked if x != completed_id]
                data["updated_at"] = _now()
                path.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
