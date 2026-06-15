from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel

from weavecode.core.events.bus import EventBus

logger = logging.getLogger(__name__)


class EventWriter:
    # 以追加模式准备 events.jsonl；父目录不存在时自动创建
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file: TextIO | None = None

    # 打开事件文件，保证异常与中断下也能被正确关闭
    async def __aenter__(self) -> EventWriter:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self._path.open("a", encoding="utf-8")
        return self

    # 关闭事件文件
    async def __aexit__(self, *args: object) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    # 把事件序列化成一行 JSON 追加写入，每写一行立即刷盘；写失败只记日志不抛异常
    async def handle(self, event: BaseModel) -> None:
        if self._file is None:
            return
        try:
            line = json.dumps(event.model_dump(), ensure_ascii=False)
            self._file.write(line + "\n")
            self._file.flush()
        except (OSError, ValueError) as e:
            logger.error("EventWriter: failed to write %s: %s", type(event).__name__, e)

    # 将 handle 注册为 bus 的订阅者
    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(self.handle)
