from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

UTC = timezone.utc
TERMINAL_BATCH_ITEM_STATUSES = {"done", "error", "stopped"}


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class MaidBatchItemInfo:
    item_id: str
    agent_name: str
    maid_request: str
    session_id: str
    status: str = "queued"
    last_assistant_output: str = ""
    result: str = ""
    error: str = ""


@dataclass(slots=True)
class MaidBatchInfo:
    batch_id: str
    unified_msg_origin: str
    sender_id: str
    maid_full_reply: str
    true_user_input: str | None
    image_urls_raw: object
    session_done_requested: bool
    reasoning_content: str = ""
    reasoning_signature: str | None = None
    status: str = "queued"
    stop_requested: bool = False
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())
    updated_at: str = field(default_factory=lambda: _utcnow().isoformat())
    items: list[MaidBatchItemInfo] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = _utcnow().isoformat()


class MaidBatchRegistry:
    """仅在批量任务活跃期间保留明细，完成后由协调器统一清理。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._batches: dict[str, MaidBatchInfo] = {}
        self._active_by_umo: dict[str, str] = {}

    @staticmethod
    def _build_item(agent_name: str, maid_request: str) -> MaidBatchItemInfo:
        return MaidBatchItemInfo(
            item_id=uuid.uuid4().hex,
            agent_name=agent_name,
            maid_request=maid_request,
            session_id=uuid.uuid4().hex,
        )

    @staticmethod
    def _refresh_batch_status(batch: MaidBatchInfo) -> None:
        statuses = [item.status for item in batch.items]
        if not statuses:
            batch.status = "queued"
            return
        if all(status == "queued" for status in statuses):
            batch.status = "queued"
            return
        if any(status == "running" for status in statuses):
            batch.status = "running"
            return
        if batch.stop_requested and all(
            status in TERMINAL_BATCH_ITEM_STATUSES for status in statuses
        ):
            batch.status = "stopped"
            return
        done_count = sum(status == "done" for status in statuses)
        error_count = sum(status == "error" for status in statuses)
        stopped_count = sum(status == "stopped" for status in statuses)
        if done_count and not error_count and not stopped_count:
            batch.status = "done"
        elif done_count:
            batch.status = "partial_done"
        elif stopped_count and not done_count:
            batch.status = "stopped"
        elif error_count:
            batch.status = "error"
        else:
            batch.status = "done"

    async def create_batch(
        self,
        *,
        batch_id: str,
        unified_msg_origin: str,
        sender_id: str,
        maid_full_reply: str,
        true_user_input: str | None,
        image_urls_raw: object,
        session_done_requested: bool,
        reasoning_content: str = "",
        reasoning_signature: str | None = None,
        items: list[dict[str, str]],
    ) -> MaidBatchInfo:
        async with self._lock:
            batch = MaidBatchInfo(
                batch_id=batch_id,
                unified_msg_origin=unified_msg_origin,
                sender_id=sender_id,
                maid_full_reply=maid_full_reply,
                true_user_input=true_user_input,
                image_urls_raw=image_urls_raw,
                session_done_requested=session_done_requested,
                reasoning_content=reasoning_content,
                reasoning_signature=reasoning_signature,
                items=[
                    self._build_item(
                        str(item.get("agent_name") or ""),
                        str(item.get("maid_request") or ""),
                    )
                    for item in items
                ],
            )
            self._batches[batch_id] = batch
            self._active_by_umo[unified_msg_origin] = batch_id
            return batch

    async def get_batch(self, batch_id: str) -> MaidBatchInfo | None:
        async with self._lock:
            return self._batches.get(batch_id)

    async def get_active_batch_by_umo(self, unified_msg_origin: str) -> MaidBatchInfo | None:
        async with self._lock:
            batch_id = self._active_by_umo.get(unified_msg_origin)
            if not batch_id:
                return None
            return self._batches.get(batch_id)

    async def mark_batch_running(self, batch_id: str) -> MaidBatchInfo | None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.status = "running"
            batch.touch()
            return batch

    async def request_stop(self, batch_id: str) -> MaidBatchInfo | None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            batch.stop_requested = True
            batch.touch()
            return batch

    async def update_item_running(
        self,
        batch_id: str,
        item_id: str,
    ) -> MaidBatchItemInfo | None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            for item in batch.items:
                if item.item_id == item_id:
                    item.status = "running"
                    batch.touch()
                    self._refresh_batch_status(batch)
                    return item
            return None

    async def update_item_assistant_output(
        self,
        batch_id: str,
        item_id: str,
        output: str,
    ) -> MaidBatchItemInfo | None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            for item in batch.items:
                if item.item_id == item_id:
                    item.last_assistant_output = output
                    batch.touch()
                    return item
            return None

    async def finish_item(
        self,
        batch_id: str,
        item_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
        agent_name: str | None = None,
    ) -> MaidBatchItemInfo | None:
        async with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            for item in batch.items:
                if item.item_id == item_id:
                    item.status = status
                    item.result = result
                    item.error = error
                    if agent_name:
                        item.agent_name = agent_name
                    batch.touch()
                    self._refresh_batch_status(batch)
                    if (
                        all(entry.status in TERMINAL_BATCH_ITEM_STATUSES for entry in batch.items)
                        and self._active_by_umo.get(batch.unified_msg_origin) == batch_id
                    ):
                        self._active_by_umo.pop(batch.unified_msg_origin, None)
                    return item
            return None

    async def discard_batch(self, batch_id: str) -> MaidBatchInfo | None:
        async with self._lock:
            batch = self._batches.pop(batch_id, None)
            if batch is None:
                return None
            if self._active_by_umo.get(batch.unified_msg_origin) == batch_id:
                self._active_by_umo.pop(batch.unified_msg_origin, None)
            return batch
