from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class MaidBackgroundTaskInfo:
    task_id: str
    unified_msg_origin: str
    sender_id: str
    agent_name: str
    maid_request: str
    status: str
    created_at: str
    updated_at: str
    last_assistant_output: str = ""
    last_progress: str = ""
    last_agent_result: str = ""
    error: str = ""

    @classmethod
    def create(
        cls,
        *,
        unified_msg_origin: str,
        sender_id: str,
        agent_name: str,
        maid_request: str,
    ) -> MaidBackgroundTaskInfo:
        now = _utcnow().isoformat()
        return cls(
            task_id=uuid.uuid4().hex,
            unified_msg_origin=unified_msg_origin,
            sender_id=sender_id,
            agent_name=agent_name,
            maid_request=maid_request,
            status="queued",
            created_at=now,
            updated_at=now,
        )

    def touch(self) -> None:
        self.updated_at = _utcnow().isoformat()


class MaidBackgroundTaskRegistry:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._tasks: dict[str, MaidBackgroundTaskInfo] = {}
        self._active_by_umo: dict[str, str] = {}

    async def create_task(
        self,
        *,
        unified_msg_origin: str,
        sender_id: str,
        agent_name: str,
        maid_request: str,
    ) -> MaidBackgroundTaskInfo:
        async with self._lock:
            info = MaidBackgroundTaskInfo.create(
                unified_msg_origin=unified_msg_origin,
                sender_id=sender_id,
                agent_name=agent_name,
                maid_request=maid_request,
            )
            self._tasks[info.task_id] = info
            self._active_by_umo[unified_msg_origin] = info.task_id
            return info

    async def mark_running(self, task_id: str, progress: str = "") -> MaidBackgroundTaskInfo | None:
        async with self._lock:
            info = self._tasks.get(task_id)
            if info is None:
                return None
            info.status = "running"
            if progress:
                info.last_progress = progress
            info.touch()
            return info

    async def update_progress(self, task_id: str, progress: str) -> MaidBackgroundTaskInfo | None:
        async with self._lock:
            info = self._tasks.get(task_id)
            if info is None:
                return None
            info.last_progress = progress
            info.touch()
            return info

    async def update_assistant_output(
        self, task_id: str, output: str
    ) -> MaidBackgroundTaskInfo | None:
        async with self._lock:
            info = self._tasks.get(task_id)
            if info is None:
                return None
            info.last_assistant_output = output
            info.touch()
            return info

    async def finish(
        self,
        task_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
    ) -> MaidBackgroundTaskInfo | None:
        async with self._lock:
            info = self._tasks.get(task_id)
            if info is None:
                return None
            info.status = status
            info.last_agent_result = result
            info.error = error
            info.touch()
            if self._active_by_umo.get(info.unified_msg_origin) == task_id:
                self._active_by_umo.pop(info.unified_msg_origin, None)
            return info

    async def get_active_by_umo(self, unified_msg_origin: str) -> MaidBackgroundTaskInfo | None:
        async with self._lock:
            task_id = self._active_by_umo.get(unified_msg_origin)
            if not task_id:
                return None
            return self._tasks.get(task_id)

    async def list_active(self) -> list[MaidBackgroundTaskInfo]:
        async with self._lock:
            result: list[MaidBackgroundTaskInfo] = []
            for task_id in self._active_by_umo.values():
                info = self._tasks.get(task_id)
                if info is not None:
                    result.append(info)
            return result
