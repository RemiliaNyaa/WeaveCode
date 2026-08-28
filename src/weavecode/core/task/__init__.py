# 任务模块门面：导出任务模型与任务管理器
#
# TaskManager 按「会话目录」构造（目录形如 <session>/.tasks），实例由 SessionManager
# 按 session 缓存复用——同一次会话的多轮对话共用同一批任务文件，编号跨 run 连续。
from weavecode.core.task.manager import TaskManager
from weavecode.core.task.model import Task

__all__ = ["Task", "TaskManager"]
