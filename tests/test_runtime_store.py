"""Functional tests for runtime_store (1.3.0 persistence layer)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from astrbot_plugin_maid_agent.runtime_store import (
    RunMeta,
    RuntimeStore,
    _read_jsonl,
)


def _make_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.runtime_store.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return RuntimeStore(config=object())


def _make_run(agent_id, task_id, umo="aiocqhttp:GroupMessage:g1", status="starting"):
    return RunMeta(
        task_id=task_id,
        agent_id=agent_id,
        unified_msg_origin=umo,
        agent_name="butler",
        sender_id="u1",
        mode="foreground",
        status=status,
        request_text="do thing",
    )


async def _create_and_load_agent(store):
    meta = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    loaded = await store.load_agent(meta.agent_id)
    assert loaded is not None
    assert loaded.agent_id == meta.agent_id
    assert loaded.agent_name == "butler"
    assert loaded.active_task_id == ""


def test_create_and_load_agent(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    asyncio.run(_create_and_load_agent(store))


async def _run_lifecycle(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    run = await store.create_run(_make_run(agent.agent_id, "a" * 32))
    await store.set_active_task(agent.agent_id, run.task_id, "starting")
    finalized = await store.finalize_run(
        agent.agent_id,
        run.task_id,
        status="completed",
        result="hello",
        error="",
    )
    assert finalized is not None
    assert finalized.status == "completed"
    assert finalized.notification is not None
    assert finalized.notification.delivered is False
    assert finalized.notification.unified_msg_origin == "aiocqhttp:GroupMessage:g1"
    assert finalized.output_file
    assert Path(finalized.output_file).read_text(encoding="utf-8") == "hello"
    notification_id = finalized.notification.notification_id

    repeated = await store.finalize_run(
        agent.agent_id,
        run.task_id,
        status="failed",
        error="late overwrite",
    )
    assert repeated is not None
    assert repeated.status == "completed"
    assert repeated.notification.notification_id == notification_id

    agent_meta = await store.load_agent(agent.agent_id)
    assert agent_meta is not None
    assert agent_meta.active_task_id == ""
    assert agent_meta.last_status == "completed"

    reloaded = await store.load_run(agent.agent_id, run.task_id)
    assert reloaded is not None
    assert reloaded.notification is not None
    assert reloaded.notification.status == "completed"


def test_run_lifecycle_atomic_finalize_with_notification(tmp_path, monkeypatch):
    asyncio.run(_run_lifecycle(_make_store(tmp_path, monkeypatch)))


async def _terminal_without_notification_is_not_refinalized(store):
    agent = await store.create_agent(
        unified_msg_origin="umo1", agent_name="butler", sender_id="u1"
    )
    run = await store.create_run(_make_run(agent.agent_id, "8" * 32))
    terminal = await store.update_run(
        agent.agent_id,
        run.task_id,
        status="failed",
        error="original",
    )
    assert terminal is not None
    assert terminal.notification is None
    ended_at = terminal.ended_at

    repeated = await store.finalize_run(
        agent.agent_id,
        run.task_id,
        status="completed",
        result="late overwrite",
    )

    assert repeated is not None
    assert repeated.status == "failed"
    assert repeated.error == "original"
    assert repeated.result == ""
    assert repeated.notification is None
    assert repeated.ended_at == ended_at


def test_terminal_without_notification_is_not_refinalized(tmp_path, monkeypatch):
    asyncio.run(_terminal_without_notification_is_not_refinalized(_make_store(tmp_path, monkeypatch)))


async def _claim_notification(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    run = await store.create_run(_make_run(agent.agent_id, "b" * 32))
    await store.finalize_run(agent.agent_id, run.task_id, status="stopped", result="x")
    claimed = await store.claim_notification(agent.agent_id, run.task_id)
    assert claimed is not None
    assert claimed.notification.delivered is True

    snap = await store.list_pending_notifications("aiocqhttp:GroupMessage:g1")
    assert all(n.notification_id != claimed.notification.notification_id for _, n in snap)


def test_claim_notification_marks_delivered(tmp_path, monkeypatch):
    asyncio.run(_claim_notification(_make_store(tmp_path, monkeypatch)))


async def _jsonl_corrupt_tail(store, tmp_path):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    await store.append_message(agent.agent_id, {"role": "user", "content": "hi"})
    await store.append_message(agent.agent_id, {"role": "assistant", "content": "yo"})
    await store.append_control(agent.agent_id, "run_start", {"task_id": "c" * 32})

    transcript_path = tmp_path / "agents" / agent.agent_id / "transcript.jsonl"
    with transcript_path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")
        fh.write('{"role":"user","content":"after-corrupt"}\n')

    records = _read_jsonl(transcript_path)
    roles = [r.get("role") for r in records if not r.get("_control")]
    assert roles == ["user", "assistant"]
    controls = [r for r in records if r.get("_control")]
    assert len(controls) == 1


def test_jsonl_append_and_corrupt_tail_truncation(tmp_path, monkeypatch):
    asyncio.run(_jsonl_corrupt_tail(_make_store(tmp_path, monkeypatch), tmp_path))


async def _run_transcript_is_sliced_by_control_markers(store):
    agent = await store.create_agent(
        unified_msg_origin="umo1", agent_name="butler", sender_id="u1"
    )
    first_task_id = "1" * 32
    second_task_id = "2" * 32
    await store.append_control(agent.agent_id, "run_start", {"task_id": first_task_id})
    await store.append_message(agent.agent_id, {"role": "user", "content": "first"})
    await store.append_control(agent.agent_id, "run_end", {"task_id": first_task_id})
    await store.append_control(agent.agent_id, "run_start", {"task_id": second_task_id})
    await store.append_message(agent.agent_id, {"role": "user", "content": "second"})
    await store.append_control(
        agent.agent_id,
        "tool_start",
        {"task_id": second_task_id, "tool_name": "search"},
    )

    first = await store.load_run_transcript(agent.agent_id, first_task_id)
    second = await store.load_run_transcript(agent.agent_id, second_task_id)

    assert [record.get("content") for record in first] == ["first"]
    assert second[0]["content"] == "second"
    assert second[1]["kind"] == "tool_start"


def test_run_transcript_is_sliced_by_control_markers(tmp_path, monkeypatch):
    asyncio.run(_run_transcript_is_sliced_by_control_markers(_make_store(tmp_path, monkeypatch)))


async def _truncate_unresolved(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    await store.append_message(
        agent.agent_id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    )
    await store.append_message(
        agent.agent_id,
        {"role": "user", "content": "after incomplete tool call"},
    )
    contexts = await store.rebuild_contexts_for_resume(agent.agent_id)
    assert contexts == []


def test_rebuild_contexts_truncates_unresolved_tool_calls(tmp_path, monkeypatch):
    asyncio.run(_truncate_unresolved(_make_store(tmp_path, monkeypatch)))


async def _keep_paired(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    await store.append_message(agent.agent_id, {"role": "user", "content": "hi"})
    await store.append_message(
        agent.agent_id,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
    )
    await store.append_message(
        agent.agent_id,
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    )
    contexts = await store.rebuild_contexts_for_resume(agent.agent_id)
    assert [c["role"] for c in contexts] == ["user", "assistant", "tool"]


def test_rebuild_contexts_keeps_paired_tool_calls(tmp_path, monkeypatch):
    asyncio.run(_keep_paired(_make_store(tmp_path, monkeypatch)))


async def _reconcile(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    run = await store.create_run(_make_run(agent.agent_id, "d" * 32, status="running"))
    orphan = await store.create_run(_make_run(agent.agent_id, "e" * 32, status="starting"))
    await store.set_active_task(agent.agent_id, run.task_id, "running")

    reconciled = await store.reconcile_on_restart()
    assert {item.task_id for item in reconciled} == {run.task_id, orphan.task_id}
    assert all(item.status == "interrupted" for item in reconciled)
    assert all(item.notification is None for item in reconciled)

    meta = await store.load_agent(agent.agent_id)
    assert meta is not None
    assert meta.active_task_id == ""
    assert meta.last_status == "interrupted"


def test_reconcile_on_restart_collapses_running_to_interrupted(tmp_path, monkeypatch):
    asyncio.run(_reconcile(_make_store(tmp_path, monkeypatch)))


async def _prune(store):
    agent = await store.create_agent(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        agent_name="butler",
        sender_id="u1",
    )
    assert await store.prune_inactive(0) == 0
    assert await store.prune_inactive(3650) == 0

    meta = await store.load_agent(agent.agent_id)
    assert meta is not None
    meta.updated_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    await store.write_agent_meta_raw(meta)
    memory_file = store.data_dir / "memory" / "keep.txt"
    legacy_session = store.data_dir / "sessions" / "legacy.json"
    unrelated = store.data_dir / "unrelated.txt"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_session.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("keep", encoding="utf-8")
    legacy_session.write_text("{}", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    removed = await store.prune_inactive(1)
    assert removed == 1
    assert await store.load_agent(agent.agent_id) is None
    assert memory_file.exists()
    assert legacy_session.exists()
    assert unrelated.exists()


def test_prune_inactive_respects_retention(tmp_path, monkeypatch):
    asyncio.run(_prune(_make_store(tmp_path, monkeypatch)))


async def _delete_agent_removes_only_runtime_agent(store):
    agent = await store.create_agent(
        unified_msg_origin="umo1", agent_name="butler", sender_id="u1"
    )
    run = await store.create_run(_make_run(agent.agent_id, "9" * 32, umo="umo1"))
    await store.append_message(agent.agent_id, {"role": "user", "content": "hello"})
    await store.finalize_run(agent.agent_id, run.task_id, status="completed", result="done")
    memory_file = store.data_dir / "memory" / "keep.txt"
    legacy_file = store.data_dir / "sessions" / "keep.json"
    memory_file.parent.mkdir(parents=True, exist_ok=True)
    legacy_file.parent.mkdir(parents=True, exist_ok=True)
    memory_file.write_text("keep", encoding="utf-8")
    legacy_file.write_text("{}", encoding="utf-8")

    assert await store.delete_agent(agent.agent_id) is True
    assert await store.load_agent(agent.agent_id) is None
    assert memory_file.exists()
    assert legacy_file.exists()
    assert await store.delete_agent(agent.agent_id) is False


def test_delete_agent_removes_only_runtime_agent(tmp_path, monkeypatch):
    asyncio.run(_delete_agent_removes_only_runtime_agent(_make_store(tmp_path, monkeypatch)))


async def _list_by_umo(store):
    a1 = await store.create_agent(
        unified_msg_origin="umo1", agent_name="butler", sender_id="u1"
    )
    a2 = await store.create_agent(
        unified_msg_origin="umo2", agent_name="butler", sender_id="u2"
    )
    r1 = await store.create_run(_make_run(a1.agent_id, "e" * 32, umo="umo1"))
    r2 = await store.create_run(_make_run(a2.agent_id, "f" * 32, umo="umo2"))
    await store.finalize_run(a1.agent_id, r1.task_id, status="completed", result="r1")
    await store.finalize_run(a2.agent_id, r2.task_id, status="failed", error="boom")

    snap1 = await store.list_pending_notifications("umo1")
    snap2 = await store.list_pending_notifications("umo2")
    assert len(snap1) == 1
    assert len(snap2) == 1
    assert snap1[0][1].status == "completed"
    assert snap2[0][1].status == "failed"


def test_list_pending_notifications_filters_by_umo(tmp_path, monkeypatch):
    asyncio.run(_list_by_umo(_make_store(tmp_path, monkeypatch)))
