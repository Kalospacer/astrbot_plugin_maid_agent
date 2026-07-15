"""Persistent terminal-notification outbox with opportunistic snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from astrbot.api import logger

from .constants import (
    MAID_NOTIFICATION_ID_META_KEY,
    MAID_NOTIFICATION_IDS_META_KEY,
)
from .runtime_store import PendingNotification, RuntimeStore

# Backward-compatible aliases (tests import these names directly).
NOTIFICATION_ID_META_KEY = MAID_NOTIFICATION_ID_META_KEY
NOTIFICATION_IDS_META_KEY = MAID_NOTIFICATION_IDS_META_KEY


class NotifierResult:
    def __init__(self, *, delivered: bool, error: str = "") -> None:
        self.delivered = delivered
        self.error = error


Notifier = Callable[[list[PendingNotification]], Awaitable[NotifierResult]]
HistoryScanner = Callable[[str], Awaitable[list[dict]]]


class NotificationOutbox:
    """Serializes one merged notification snapshot per UMO."""

    def __init__(self, store: RuntimeStore, *, notifier: Notifier | None = None) -> None:
        self.store = store
        self._notifier = notifier
        self._history_scanner: HistoryScanner | None = None
        self._guard = asyncio.Lock()
        self._in_flight: set[str] = set()
        self._pending_redelivery: set[str] = set()
        self._tasks: set[asyncio.Task] = set()

    def set_notifier(self, notifier: Notifier) -> None:
        self._notifier = notifier

    def set_history_scanner(self, scanner: HistoryScanner) -> None:
        self._history_scanner = scanner

    async def queue_delivery(self, unified_msg_origin: str) -> None:
        if not unified_msg_origin or self._notifier is None:
            return
        async with self._guard:
            if unified_msg_origin in self._in_flight:
                self._pending_redelivery.add(unified_msg_origin)
                return
            self._in_flight.add(unified_msg_origin)
            task = asyncio.create_task(
                self._deliver_pass(unified_msg_origin),
                name=f"maid-notify-{abs(hash(unified_msg_origin))}",
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def note_user_message(self, unified_msg_origin: str) -> None:
        await self.queue_delivery(unified_msg_origin)

    async def note_result_claimed(self, agent_id: str, task_id: str) -> None:
        run = await self.store.load_run(agent_id, task_id)
        await self.store.claim_notification(agent_id, task_id)
        if run is not None:
            await self.queue_delivery(run.unified_msg_origin)

    async def on_restart(self) -> None:
        umos: set[str] = set()
        for agent_id in await self.store.list_agent_ids():
            for run in await self.store.list_runs(agent_id):
                if run.notification is not None and not run.notification.delivered:
                    umos.add(run.unified_msg_origin)
        for umo in umos:
            await self.queue_delivery(umo)

    async def wait_for_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def shutdown(self) -> None:
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._in_flight.clear()
        self._pending_redelivery.clear()

    async def _deliver_pass(self, unified_msg_origin: str) -> None:
        try:
            await self._deliver_snapshot(unified_msg_origin)
        finally:
            schedule_again = False
            async with self._guard:
                self._in_flight.discard(unified_msg_origin)
                if unified_msg_origin in self._pending_redelivery:
                    self._pending_redelivery.discard(unified_msg_origin)
                    schedule_again = True
            if schedule_again:
                await self.queue_delivery(unified_msg_origin)

    async def _deliver_snapshot(self, unified_msg_origin: str) -> None:
        if self._notifier is None:
            return
        snapshot = await self.store.list_pending_notifications(unified_msg_origin)
        if not snapshot:
            return
        history = await self._read_history(unified_msg_origin)
        pending: list[PendingNotification] = []
        for _run, notification in snapshot:
            if self._history_contains(history, notification.notification_id):
                # A persisted history marker is evidence that this notification
                # already reached the conversation. If a wake failed before
                # persistence, the marker is absent and the item stays pending.
                await self.store.claim_notification(
                    notification.agent_id,
                    notification.task_id,
                )
            else:
                pending.append(notification)
        if not pending:
            return
        logger.info(
            "[大小姐模式] notification snapshot 投递: umo=%s pending=%d",
            unified_msg_origin,
            len(pending),
        )
        try:
            result = await self._notifier(pending)
        except Exception as exc:
            logger.error(
                "[大小姐模式] notification snapshot 投递异常: umo=%s err=%s",
                unified_msg_origin,
                exc,
                exc_info=True,
            )
            return
        if not result.delivered:
            if result.error:
                logger.warning(
                    "[大小姐模式] notification 投递失败，等待下次触发: umo=%s err=%s",
                    unified_msg_origin,
                    result.error,
                )
            return
        for notification in pending:
            await self.store.claim_notification(notification.agent_id, notification.task_id)

    async def _read_history(self, unified_msg_origin: str) -> list[dict]:
        if self._history_scanner is None:
            return []
        try:
            history = await self._history_scanner(unified_msg_origin)
        except Exception as exc:
            logger.warning(
                "[大小姐模式] 读取会话历史用于 notification 去重失败: umo=%s err=%s",
                unified_msg_origin,
                exc,
            )
            return []
        return history if isinstance(history, list) else []

    @staticmethod
    def _history_contains(history: list[dict], notification_id: str) -> bool:
        for message in history:
            if not isinstance(message, dict):
                continue
            if message.get(NOTIFICATION_ID_META_KEY) == notification_id:
                return True
            values = message.get(NOTIFICATION_IDS_META_KEY)
            if isinstance(values, list) and notification_id in values:
                return True
        return False
