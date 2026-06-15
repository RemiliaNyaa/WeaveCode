from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel

EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    # 注册一个事件处理函数
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    async def publish(self, event: BaseModel) -> None:
        for handler in self._subscribers:
            await handler(event)
