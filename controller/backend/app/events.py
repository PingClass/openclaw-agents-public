"""SSE 用的事件匯流排（行程內，單一 worker）。

事件內容只放 id、狀態等，不放業務資料與個資。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from typing import Any


class EventBus:
    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue_size = queue_size
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        with self._lock:
            seq = next(self._seq)
        event = {"id": seq, "type": event_type, "data": data}
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is not None and running is not self._loop:
            # 從其他執行緒（例如 CLI 或 threadpool）發佈時，交回事件迴圈處理
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._deliver, event)
            return
        self._deliver(event)

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 前端太慢：丟掉最舊的一筆，前端會靠輪詢補齊
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
