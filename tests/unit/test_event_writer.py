from __future__ import annotations

import json
from pathlib import Path

from weavecode.core.bus.events import RunStartedEvent
from weavecode.core.events.bus import EventBus
from weavecode.core.events.writer import EventWriter


# 读出事件文件里的每一行，反序列化成事件载荷
async def _rows(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


# 功能：验证 handle 后事件被写进事件文件（type 与关键字段都对）
# 设计：用真实临时文件而非 mock，因为 EventWriter 的核心职责是落盘，只有真读出来才能证明写入正确
async def test_event_writer_writes_event_row(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    event = RunStartedEvent(run_id="run-1", goal="test goal", ts="2026-05-11T00:00:00Z")

    async with EventWriter(path) as writer:
        await writer.handle(event)

    rows = await _rows(path)
    assert len(rows) == 1
    stored = rows[0]
    assert stored["type"] == "run.started"
    assert stored["run_id"] == "run-1"
    assert stored["goal"] == "test goal"


# 功能：验证 subscribe 把 writer 接入 EventBus 后，bus.publish 能触发落盘
# 设计：通过 bus.publish 触发写入（而非直接调 writer.handle），测试集成路径，确认订阅接线正确
async def test_event_writer_subscribe_via_bus(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    bus = EventBus()
    event = RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z")

    async with EventWriter(path) as writer:
        writer.subscribe(bus)
        await bus.publish(event)

    rows = await _rows(path)
    assert [r["type"] for r in rows] == ["run.started"]


# 功能：验证未通过 async with 打开时 handle 静默返回、不落盘也不抛异常
# 设计：直接实例化 writer（跳过 async with），以「不引发异常且文件不存在」为判据；对应 EventWriter 的防御性设计
async def test_event_writer_handle_when_not_open_is_noop(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    writer = EventWriter(path)
    await writer.handle(RunStartedEvent(run_id="r1", goal="g", ts="2026-05-11T00:00:00Z"))
    assert not path.exists()
