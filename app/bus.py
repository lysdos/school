"""线程安全的 SSE 事件广播。

消费线程通过 publish_threadsafe() 投递，事件在 asyncio 事件循环里分发给各订阅者。
订阅者队列满时丢弃的行为是刻意的：慢客户端不应该拖住读卡链路。
"""

from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    def __init__(self, maxsize: int = 100):
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish_threadsafe(self, item: Any) -> None:
        """可从任意线程调用。未绑定事件循环时静默丢弃（例如纯 CLI 场景）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._publish, item)
        except RuntimeError:
            pass

    def _publish(self, item: Any) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass
