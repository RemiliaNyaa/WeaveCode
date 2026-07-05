# 任务模块门面：导出任务模型与任务管理器
from weavecode.core.task.manager import TaskManager
from weavecode.core.task.model import Task

__all__ = ["Task", "TaskManager"]
