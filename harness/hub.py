"""events.mux / events.host 帧扇出（对应 apiproxy 的两条下行流）。

帧以 ServerRequest 全形推给所有订阅者；SSE 载体格式：
``data: {json}\\n\\n``。订阅建立时由调用方重放基线（session/subscribed、
队列快照等）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

QUEUE_SIZE = 500


class StreamHub:
    def __init__(self, name: str = "mux"):
        self.name = name
        self._queues: list[asyncio.Queue] = []

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._queues.append(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        with _suppress_value_error():
            self._queues.remove(queue)

    def publish(self, payload: dict) -> None:
        """非阻塞扇出；队列满时丢最旧（实时流以重连/重拉为收敛手段）。"""
        for queue in list(self._queues):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                with _suppress_value_error():
                    while True:
                        queue.get_nowait()
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    async def close(self) -> None:
        for queue in list(self._queues):
            closed = {"type": "stream/error", "error": {"code": "cancelled", "message": "宿主已关闭", "details": {}}}
            try:
                queue.put_nowait(closed)
            except asyncio.QueueFull:
                pass
        self._queues.clear()


class _suppress_value_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is ValueError


def sse_frame(payload: dict) -> str:
    """ServerRequest 全形 → SSE data 行。rpcId 由推送方填好。"""
    frame = dict(payload)
    frame.setdefault("type", "server-request")
    frame.setdefault("rpcId", str(uuid.uuid4()))
    return f"data: {json.dumps(frame, ensure_ascii=False, default=str)}\n\n"
