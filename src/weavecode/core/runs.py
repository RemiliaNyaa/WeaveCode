from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

# 每次 run 的事件与中间产物所在根目录
RUNS_DIR = Path("~/.weave/runs").expanduser()


# 生成格式为 YYYYMMDD-HHMMSS-xxxxxx 的唯一 run ID
def new_run_id() -> str:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    suffix = uuid.uuid4().hex[:6]
    return f"{ts}-{suffix}"


# 返回某次 run 的目录
def run_dir(run_id: str) -> Path:
    return RUNS_DIR / run_id


# 返回某次 run 的事件文件路径
def events_file(run_id: str) -> Path:
    return run_dir(run_id) / "events.jsonl"


# 创建某次 run 的目录（已存在时静默返回），返回该目录
def ensure_run_dir(run_id: str) -> Path:
    path = run_dir(run_id)
    path.mkdir(parents=True, exist_ok=True)
    return path
