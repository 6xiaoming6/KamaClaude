from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

type EventHandler = Callable[[BaseModel], Awaitable[None]]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    # 注册一个事件处理函数
    # 当前总线会把每个事件交给全部订阅者，没有按事件类型筛选
    def subscribe(self, handler: EventHandler) -> None:
        self._subscribers.append(handler)

    # 按注册顺序依次调用所有订阅者
    # 把事件交给所有的订阅者
    async def publish(self, event: BaseModel) -> None:
        for handler in self._subscribers:
            await handler(event)

    # 释放订阅者，避免短生命周期写入器被共享总线保留
    def unsubscribe(self, handler: EventHandler) -> None:
        self._subscribers = [h for h in self._subscribers if h != handler]

    # 单独隔离观察者异常，通知失败不得改变已提交的执行结果
    async def publish_safe(self, event: BaseModel) -> None:
        for handler in list(self._subscribers):
            try:
                await handler(event)
            except Exception:
                logging.getLogger(__name__).exception("event observer failed")
