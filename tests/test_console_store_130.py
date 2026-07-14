"""Tests for 1.3.0 console_store agent/run hierarchy fields."""

from __future__ import annotations

import asyncio

from astrbot_plugin_maid_agent.console_store import ConsoleTaskPatch, MaidConsoleEventStore


def _make_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.console_store.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return MaidConsoleEventStore()


async def _agent_fields_persisted_in_meta(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    await store.initialize()
    patch = ConsoleTaskPatch(
        task_id="t" + "1" * 31,
        kind="single",
        source="chat",
        unified_msg_origin="umo:a:1",
        sender_id="owner",
        agent_name="butler",
        status="running",
        request_text="do thing",
        agent_id="a" * 32,
        run_mode="background",
        background_reason="timeout",
        notification_id="n" * 32,
    )
    await store.ensure_task(patch)
    task = await store.get_task(patch.task_id)
    assert task is not None
    meta = task["meta"]
    assert meta["agent_id"] == "a" * 32
    assert meta["run_mode"] == "background"
    assert meta["background_reason"] == "timeout"
    assert meta["notification_id"] == "n" * 32
    await store.close()


def test_agent_fields_persisted_in_meta(tmp_path, monkeypatch):
    asyncio.run(_agent_fields_persisted_in_meta(tmp_path, monkeypatch))


async def _list_agents_groups_by_agent_id(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    await store.initialize()
    # Two tasks for agent A, one for agent B.
    for tid_suffix, agent_id, status in [
        ("1", "a" * 32, "running"),
        ("2", "a" * 32, "completed"),
        ("3", "b" * 32, "running"),
    ]:
        patch = ConsoleTaskPatch(
            task_id="t" + tid_suffix + "0" * 30,
            kind="single",
            source="chat",
            unified_msg_origin="umo:a:1",
            sender_id="owner",
            agent_name="butler",
            status=status,
            request_text=f"task {tid_suffix}",
            agent_id=agent_id,
            run_mode="background",
        )
        await store.ensure_task(patch)
    agents = await store.list_agents("umo:a:1")
    agent_ids = {a["agent_id"] for a in agents}
    assert agent_ids == {"a" * 32, "b" * 32}
    # The most recently updated task wins the summary.
    a_summary = next(a for a in agents if a["agent_id"] == "a" * 32)
    assert a_summary["last_status"] in {"running", "completed"}
    await store.close()


def test_list_agents_groups_by_agent_id(tmp_path, monkeypatch):
    asyncio.run(_list_agents_groups_by_agent_id(tmp_path, monkeypatch))


async def _get_agent_runs_returns_all_for_agent(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    await store.initialize()
    for tid_suffix, agent_id in [("1", "a" * 32), ("2", "a" * 32), ("3", "b" * 32)]:
        patch = ConsoleTaskPatch(
            task_id="t" + tid_suffix + "0" * 30,
            kind="single",
            source="chat",
            unified_msg_origin="umo:a:1",
            sender_id="owner",
            agent_name="butler",
            status="running",
            request_text=f"task {tid_suffix}",
            agent_id=agent_id,
            run_mode="foreground" if tid_suffix == "1" else "background",
        )
        await store.ensure_task(patch)
    runs = await store.get_agent_runs("a" * 32)
    assert len(runs) == 2
    for run in runs:
        assert run["agent_id"] == "a" * 32
        assert "run_mode" in run
    await store.close()


def test_get_agent_runs_returns_all_for_agent(tmp_path, monkeypatch):
    asyncio.run(_get_agent_runs_returns_all_for_agent(tmp_path, monkeypatch))


async def _interrupted_status_sets_completed_at(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)
    await store.initialize()
    patch = ConsoleTaskPatch(
        task_id="t" + "9" * 31,
        kind="single",
        source="system",
        unified_msg_origin="umo:a:1",
        sender_id="owner",
        agent_name="butler",
        status="interrupted",
        request_text="orphan",
        agent_id="c" * 32,
    )
    await store.ensure_task(patch)
    task = await store.get_task(patch.task_id)
    assert task is not None
    assert task["completed_at"] != ""
    await store.close()


def test_interrupted_status_sets_completed_at(tmp_path, monkeypatch):
    asyncio.run(_interrupted_status_sets_completed_at(tmp_path, monkeypatch))
