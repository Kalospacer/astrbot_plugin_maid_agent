"""大小姐管家模式插件 - WebUI 控制台 SSE 广播管道。

替代原 console_store 的 SQLite 审计存储：1.4.0 起控制台数据全部来自
runtime_store 的 transcript，这里只保留轻量的内存 pub/sub，供
``console/stream`` 向前端推送 runtime_trace / reset / closed 消息。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from .time_utils import iso_now


class SseHub:
    """In-memory fan-out of console SSE messages to all subscribed queues."""

    def __init__(self, *, queue_size: int = 200) -> None:
        self._queue_size = queue_size
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._subscriber_lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        async with self._subscriber_lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._subscriber_lock:
            self._subscribers.discard(queue)

    async def publish(self, item: dict[str, Any]) -> None:
        """Broadcast one message to every subscriber; slow queues drop oldest."""
        async with self._subscriber_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(item)

    async def close(self) -> None:
        """Notify all subscribers that the stream is ending (plugin shutdown)."""
        async with self._subscriber_lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait({"type": "closed", "created_at": iso_now()})
