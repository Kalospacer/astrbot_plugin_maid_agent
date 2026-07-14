"""Functional tests for notification_outbox (1.3.0 delivery layer)."""

from __future__ import annotations

import asyncio

from astrbot_plugin_maid_agent.notification_outbox import (
    NOTIFICATION_ID_META_KEY,
    NotificationOutbox,
    NotifierResult,
)
from astrbot_plugin_maid_agent.runtime_store import RunMeta, RuntimeStore


def _make_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.runtime_store.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return RuntimeStore(config=object())


def _make_run(agent_id, task_id, umo="umo1"):
    return RunMeta(
        task_id=task_id,
        agent_id=agent_id,
        unified_msg_origin=umo,
        agent_name="butler",
        sender_id="u1",
        mode="background",
        status="starting",
        request_text="x",
    )


async def _finalize(store, agent_id, task_id, status="completed", result="r"):
    await store.finalize_run(agent_id, task_id, status=status, result=result)


async def _first_delivery_claims_pending(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    async def notifier(notifications):
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    agent = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    run = await store.create_run(_make_run(agent.agent_id, "a" * 32))
    await _finalize(store, agent.agent_id, run.task_id)

    await outbox.queue_delivery("umo1")
    await outbox.wait_for_idle()

    assert delivered == [run.task_id]
    claimed = await store.load_run(agent.agent_id, run.task_id)
    assert claimed is not None
    assert claimed.notification.delivered is True


def test_first_delivery_claims_pending(tmp_path, monkeypatch):
    asyncio.run(_first_delivery_claims_pending(tmp_path, monkeypatch))


async def _snapshot_merges_all_pending(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    snapshots: list[list[str]] = []

    async def notifier(notifications):
        task_ids = [item.task_id for item in notifications]
        snapshots.append(task_ids)
        delivered.extend(task_ids)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    a1 = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    a2 = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    r1 = await store.create_run(_make_run(a1.agent_id, "b" * 32))
    r2 = await store.create_run(_make_run(a2.agent_id, "c" * 32))
    await _finalize(store, a1.agent_id, r1.task_id, result="r1")
    await _finalize(store, a2.agent_id, r2.task_id, result="r2")

    await outbox.queue_delivery("umo1")
    await outbox.wait_for_idle()
    assert sorted(delivered) == sorted([r1.task_id, r2.task_id])
    assert len(snapshots) == 1
    assert sorted(snapshots[0]) == sorted([r1.task_id, r2.task_id])


def test_snapshot_merges_all_pending(tmp_path, monkeypatch):
    asyncio.run(_snapshot_merges_all_pending(tmp_path, monkeypatch))


async def _new_completion_during_delivery_picked_up_next(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []
    block_notifier = asyncio.Event()

    async def notifier(notifications):
        # Block the first delivery so a new completion can land mid-flight.
        await block_notifier.wait()
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    a1 = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    r1 = await store.create_run(_make_run(a1.agent_id, "d" * 32))
    await _finalize(store, a1.agent_id, r1.task_id)

    await outbox.queue_delivery("umo1")
    await asyncio.sleep(0.02)
    # While delivery is blocked, finalize a second run.
    a2 = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    r2 = await store.create_run(_make_run(a2.agent_id, "e" * 32))
    await _finalize(store, a2.agent_id, r2.task_id)
    # Trigger a second queue_delivery — should be deferred (in flight) then
    # re-scheduled after the first pass completes.
    await outbox.queue_delivery("umo1")
    await asyncio.sleep(0.02)

    block_notifier.set()
    await outbox.wait_for_idle()
    assert sorted(delivered) == sorted([r1.task_id, r2.task_id])


def test_new_completion_during_delivery_picked_up_next(tmp_path, monkeypatch):
    asyncio.run(_new_completion_during_delivery_picked_up_next(tmp_path, monkeypatch))


async def _best_effort_dedupe_via_history(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    async def notifier(notifications):
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)

    agent = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    run = await store.create_run(_make_run(agent.agent_id, "f" * 32))
    await _finalize(store, agent.agent_id, run.task_id)

    # Pretend the main agent already received this notification (history
    # contains its notification_id marker).
    loaded = await store.load_run(agent.agent_id, run.task_id)
    assert loaded is not None
    assert loaded.notification is not None
    history = [{"role": "assistant", "content": "x", NOTIFICATION_ID_META_KEY: loaded.notification.notification_id}]

    async def scanner(_umo):
        return history

    outbox.set_history_scanner(scanner)
    await outbox.queue_delivery("umo1")
    await outbox.wait_for_idle()
    # Notifier was NOT called (already delivered) -> claimed silently.
    assert delivered == []
    claimed = await store.load_run(agent.agent_id, run.task_id)
    assert claimed is not None
    assert claimed.notification.delivered is True


def test_best_effort_dedupe_via_history(tmp_path, monkeypatch):
    asyncio.run(_best_effort_dedupe_via_history(tmp_path, monkeypatch))


async def _no_periodic_retry_only_on_triggers(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    async def notifier(notifications):
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    agent = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    run = await store.create_run(_make_run(agent.agent_id, "1" * 32))

    # Failing delivery: notifier returns not-delivered.
    async def failing_notifier(_notifications):
        return NotifierResult(delivered=False, error="main agent busy")

    outbox.set_notifier(failing_notifier)
    await _finalize(store, agent.agent_id, run.task_id)
    await outbox.queue_delivery("umo1")
    await outbox.wait_for_idle()
    assert delivered == []  # nothing delivered

    # No timer retry. After a while still nothing.
    await outbox.wait_for_idle()
    assert delivered == []

    # A new user message triggers retry, and now it succeeds.
    outbox.set_notifier(notifier)
    await outbox.note_user_message("umo1")
    await asyncio.sleep(0.05)
    assert delivered == [run.task_id]


def test_no_periodic_retry_only_on_triggers(tmp_path, monkeypatch):
    asyncio.run(_no_periodic_retry_only_on_triggers(tmp_path, monkeypatch))


async def _result_claim_skips_current_and_retries_other_pending(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    async def notifier(notifications):
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    agent = await store.create_agent(unified_msg_origin="umo1", agent_name="butler", sender_id="u1")
    run = await store.create_run(_make_run(agent.agent_id, "2" * 32))
    await _finalize(store, agent.agent_id, run.task_id)
    other_agent = await store.create_agent(
        unified_msg_origin="umo1", agent_name="butler", sender_id="u1"
    )
    other_run = await store.create_run(_make_run(other_agent.agent_id, "3" * 32))
    await _finalize(store, other_agent.agent_id, other_run.task_id)

    # maid_task(result) claims this notification, then retries other pending
    # notifications for the same UMO.
    await outbox.note_result_claimed(agent.agent_id, run.task_id)
    await outbox.wait_for_idle()
    assert delivered == [other_run.task_id]


def test_result_claim_skips_delivery(tmp_path, monkeypatch):
    asyncio.run(_result_claim_skips_current_and_retries_other_pending(tmp_path, monkeypatch))


async def _restart_retries_pending(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    delivered: list[str] = []

    async def notifier(notifications):
        delivered.extend(item.task_id for item in notifications)
        return NotifierResult(delivered=True)

    outbox = NotificationOutbox(store, notifier=notifier)
    agent = await store.create_agent(
        unified_msg_origin="umo-restart",
        agent_name="butler",
        sender_id="u1",
    )
    run = await store.create_run(_make_run(agent.agent_id, "4" * 32, "umo-restart"))
    await _finalize(store, agent.agent_id, run.task_id)
    await outbox.on_restart()
    await outbox.wait_for_idle()
    assert delivered == [run.task_id]


def test_restart_retries_pending(tmp_path, monkeypatch):
    asyncio.run(_restart_retries_pending(tmp_path, monkeypatch))
