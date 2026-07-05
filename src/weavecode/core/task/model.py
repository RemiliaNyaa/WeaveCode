from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

# 任务状态取值
TASK_STATUSES: tuple[str, ...] = ("pending", "in_progress", "completed", "cancelled")


def _now() -> str:
    return datetime.now(UTC).isoformat()


# 任务的六字段模型：subject、description、status、blocked_by、created_at、updated_at
@dataclass
class Task:
    id: int
    subject: str
    description: str = ""
    status: str = "pending"
    blocked_by: list[int] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    # 序列化成 task_{id}.json 的内容
    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blocked_by": list(self.blocked_by),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
