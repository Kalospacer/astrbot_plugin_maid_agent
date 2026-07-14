from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import astrbot_plugin_maid_agent.console_store as console_store_module
import astrbot_plugin_maid_agent.main as main_module
import pytest
from astrbot_plugin_maid_agent.background_registry import (
    MaidBackgroundTaskRegistry,
    MaidTaskConflictError,
)
from astrbot_plugin_maid_agent.batch_registry import MaidBatchRegistry
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.console_store import ConsoleTaskPatch, MaidConsoleEventStore
from astrbot_plugin_maid_agent.main import MaidAgent


class _ConsoleStore:
    def __init__(self) -> None:
        self.tasks: dict[str, dict] = {}
        self.events: list[dict] = []

    async def ensure_task(self, patch):
        task = asdict(patch)
        self.tasks[patch.task_id] = task
        return task

    async def record_event(self, **payload):
        self.events.append(payload)
        return payload

    async def update_task_status(self, task_id: str, status: str, *, meta=None):
        task = self.tasks.get(task_id)
        if task is not None:
            task["status"] = status
            if meta:
                task.setdefault("meta", {}).update(meta)
        return task


class _Event:
    def __init__(self, sender_id: str = "owner") -> None:
        self.unified_msg_origin = "platform:GroupMessage:group"
        self.message_obj = SimpleNamespace(message_id="message-1")
        self.sender_id = sender_id
        self.sent: list[str] = []
        self.extras: dict[str, object] = {}

    def get_sender_id(self) -> str:
        return self.sender_id

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default=None):
        return self.extras.get(key, default)

    async def send(self, chain) -> None:
        self.sent.append(chain.get_plain_text())


def _make_plugin(config: MaidModeConfig | None = None) -> MaidAgent:
    plugin = object.__new__(MaidAgent)
    plugin.maid_mode_config = config or MaidModeConfig()
    plugin.background_tasks = MaidBackgroundTaskRegistry()
    plugin.batch_registry = MaidBatchRegistry()
    plugin.console_store = _ConsoleStore()
    plugin._active_asyncio_tasks = set()
    return plugin


def test_registry_reserves_umo_atomically() -> None:
    async def scenario() -> None:
        registry = MaidBackgroundTaskRegistry()
        first = await registry.create_task(
            unified_msg_origin="umo",
            sender_id="owner",
            agent_name="butler",
            maid_request="first",
        )
        with pytest.raises(MaidTaskConflictError) as raised:
            await registry.create_task(
                unified_msg_origin="umo",
                sender_id="owner",
                agent_name="butler",
                maid_request="second",
            )
        assert raised.value.active_task.task_id == first.task_id

    asyncio.run(scenario())


def test_unified_launcher_assigns_provenance_and_rejects_race() -> None:
    async def scenario() -> None:
        plugin = _make_plugin()
        event = _Event()

        async def no_op_runner(*_args, **_kwargs) -> None:
            return None

        plugin._run_maid_follow_up_background_task = no_op_runner
        pending = {
            "agent_name": "butler",
            "maid_request": "处理任务",
            "maid_full_reply": "我让管家看看。",
        }
        task_id = await plugin._launch_maid_dispatch(
            source="chat",
            event=event,
            req=SimpleNamespace(),
            pending=pending,
            kind="single",
        )
        await asyncio.sleep(0)

        assert pending["agent_id"] == task_id
        assert pending["task_id"] == task_id
        assert pending["parent_message_id"] == "message-1"
        assert plugin.console_store.tasks[task_id]["meta"]["agent_id"] == task_id

        with pytest.raises(MaidTaskConflictError):
            await plugin._launch_maid_dispatch(
                source="chat",
                event=event,
                req=SimpleNamespace(),
                pending={"agent_name": "butler", "maid_request": "并发任务"},
                kind="single",
            )

    asyncio.run(scenario())


def test_unified_launcher_covers_batch_and_dashboard() -> None:
    async def no_op_runner(*_args, **_kwargs) -> None:
        return None

    async def scenario() -> None:
        batch_plugin = _make_plugin()
        batch_plugin._run_maid_batch_background_task = no_op_runner
        batch_event = _Event()
        batch_pending = {
            "items": [
                {"agent_name": "butler", "maid_request": "first"},
                {"agent_name": "butler", "maid_request": "second"},
            ]
        }
        batch_id = await batch_plugin._launch_maid_dispatch(
            source="chat",
            event=batch_event,
            req=SimpleNamespace(),
            pending=batch_pending,
            kind="batch",
        )
        assert batch_pending["batch_id"] == batch_id
        assert batch_pending["agent_id"] == batch_id
        assert batch_plugin.console_store.tasks[batch_id]["kind"] == "batch"

        dashboard_plugin = _make_plugin()
        dashboard_plugin._run_dashboard_dispatch_background_task = no_op_runner
        dashboard_pending = {
            "agent_name": "butler",
            "maid_request": "dashboard task",
        }
        dashboard_id = await dashboard_plugin._launch_maid_dispatch(
            source="dashboard",
            event=_Event(sender_id="ignored"),
            req=None,
            pending=dashboard_pending,
            kind="single",
        )
        assert dashboard_pending["task_id"] == dashboard_id
        assert dashboard_plugin.console_store.tasks[dashboard_id]["source"] == "dashboard"
        assert dashboard_plugin.console_store.tasks[dashboard_id]["sender_id"] == "dashboard"
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_auto_background_watchdog_notifies_without_changing_status() -> None:
    async def scenario() -> None:
        config = MaidModeConfig(
            dispatch_auto_background_enabled=True,
            dispatch_auto_background_seconds=0.01,
        )
        plugin = _make_plugin(config)
        event = _Event()
        task = await plugin.background_tasks.create_task(
            unified_msg_origin=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            agent_name="butler",
            maid_request="long task",
        )
        await plugin.background_tasks.mark_running(task.task_id)

        watchdog = plugin._start_dispatch_watchdog(
            task_id=task.task_id,
            source="chat",
            event=event,
        )
        assert watchdog is not None
        await watchdog

        current = await plugin.background_tasks.get_active_by_umo(event.unified_msg_origin)
        assert current is not None
        assert current.status == "running"
        assert plugin.console_store.events[-1]["event_type"] == "auto_background"
        assert event.sent

    asyncio.run(scenario())


def test_auto_background_watchdog_is_cancelled_for_fast_task() -> None:
    async def scenario() -> None:
        config = MaidModeConfig(
            dispatch_auto_background_enabled=True,
            dispatch_auto_background_seconds=1,
        )
        plugin = _make_plugin(config)
        event = _Event()
        task = await plugin.background_tasks.create_task(
            unified_msg_origin=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            agent_name="butler",
            maid_request="fast task",
        )
        watchdog = plugin._start_dispatch_watchdog(
            task_id=task.task_id,
            source="chat",
            event=event,
        )
        await plugin._cancel_dispatch_watchdog(watchdog)
        assert plugin.console_store.events == []
        assert event.sent == []

    asyncio.run(scenario())


def test_chat_user_cannot_stop_another_senders_task() -> None:
    async def scenario() -> None:
        plugin = _make_plugin()
        owner_event = _Event(sender_id="owner")
        task = await plugin.background_tasks.create_task(
            unified_msg_origin=owner_event.unified_msg_origin,
            sender_id="owner",
            agent_name="butler",
            maid_request="owner task",
        )
        intruder_event = _Event(sender_id="intruder")

        result = await plugin._request_stop_background_tasks(intruder_event)
        assert "不属于" in result
        current = await plugin.background_tasks.get_active_by_umo(intruder_event.unified_msg_origin)
        assert current is not None
        assert current.task_id == task.task_id

    asyncio.run(scenario())


def test_legacy_done_is_stateless_noop_for_idle_session() -> None:
    class _SessionStore:
        def __init__(self) -> None:
            self.closed = False

        async def get_active_session(self, _umo: str):
            return SimpleNamespace(owner_sender_id="owner")

        async def close_active_session(self, _umo: str, _status: str):
            self.closed = True

    async def scenario() -> None:
        plugin = _make_plugin()
        plugin.session_store = _SessionStore()
        intruder_event = _Event(sender_id="intruder")

        result = await plugin.call_maid(intruder_event, action="done")

        assert "无需显式结束" in result
        assert plugin.session_store.closed is False

    asyncio.run(scenario())


def test_restart_reconciliation_closes_orphaned_console_tasks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        console_store_module.StarTools,
        "get_data_dir",
        lambda _plugin_name: tmp_path,
    )

    async def scenario() -> None:
        store = MaidConsoleEventStore()
        await store.initialize()
        await store.ensure_task(
            ConsoleTaskPatch(
                task_id="orphan-task",
                unified_msg_origin="umo",
                sender_id="owner",
                agent_name="butler",
                status="running",
                request_text="unfinished",
            )
        )

        reconciled = await store.reconcile_incomplete_tasks()
        assert reconciled == ["orphan-task"]
        task = await store.get_task("orphan-task")
        assert task is not None
        assert task["status"] == "stopped"
        assert task["meta"]["recovered_after_restart"] is True
        events = await store.get_task_events("orphan-task")
        assert events[-1]["event_type"] == "interrupted"

    asyncio.run(scenario())


def test_cancelled_dashboard_runner_converges_to_stopped(monkeypatch) -> None:
    started = asyncio.Event()

    async def stalled_dispatch(**_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(main_module, "dispatch_to_maid_agent", stalled_dispatch)

    class _SessionStore:
        config = MaidModeConfig()

        async def close_active_session(self, _umo: str, status: str):
            assert status == "stopped"

    async def scenario() -> None:
        plugin = _make_plugin()
        plugin.context = SimpleNamespace()
        plugin.session_store = _SessionStore()
        pending = {"agent_name": "butler", "maid_request": "long dashboard task"}
        task_id = await plugin._launch_maid_dispatch(
            source="dashboard",
            event=_Event(),
            req=None,
            pending=pending,
            kind="single",
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        runner_task = next(task for task in plugin._active_asyncio_tasks if not task.done())
        runner_task.cancel()
        await asyncio.gather(runner_task, return_exceptions=True)

        current = await plugin.background_tasks.get_active_by_umo("platform:GroupMessage:group")
        assert current is None
        assert plugin.console_store.tasks[task_id]["status"] == "stopped"

    asyncio.run(scenario())


def test_stop_exception_converges_single_runner_to_stopped(monkeypatch) -> None:
    async def stopped_dispatch(**kwargs):
        kwargs["event"].set_extra("agent_stop_requested", True)
        raise RuntimeError("runner stopped before final response")

    monkeypatch.setattr(main_module, "dispatch_to_maid_agent", stopped_dispatch)

    class _SessionStore:
        config = MaidModeConfig()

        async def close_active_session(self, _umo: str, status: str):
            assert status == "stopped"

    async def empty_follow_up(**_kwargs):
        return SimpleNamespace(completion_text="", result_chain=None)

    async def scenario() -> None:
        plugin = _make_plugin()
        plugin.context = SimpleNamespace()
        plugin.session_store = _SessionStore()
        plugin._request_maid_follow_up = empty_follow_up
        event = _Event()
        pending = {"agent_name": "butler", "maid_request": "stoppable chat task"}

        task_id = await plugin._launch_maid_dispatch(
            source="chat",
            event=event,
            req=SimpleNamespace(),
            pending=pending,
            kind="single",
        )
        runner_task = next(task for task in plugin._active_asyncio_tasks if not task.done())
        await runner_task

        assert plugin.console_store.tasks[task_id]["status"] == "stopped"

    asyncio.run(scenario())


def test_stop_exception_converges_dashboard_runner_to_stopped(monkeypatch) -> None:
    async def stopped_dispatch(**kwargs):
        kwargs["event"].set_extra("agent_stop_requested", True)
        raise RuntimeError("runner stopped before final response")

    monkeypatch.setattr(main_module, "dispatch_to_maid_agent", stopped_dispatch)

    class _SessionStore:
        config = MaidModeConfig()

        async def close_active_session(self, _umo: str, status: str):
            assert status == "stopped"

    async def scenario() -> None:
        plugin = _make_plugin()
        plugin.context = SimpleNamespace()
        plugin.session_store = _SessionStore()
        pending = {"agent_name": "butler", "maid_request": "stoppable dashboard task"}

        task_id = await plugin._launch_maid_dispatch(
            source="dashboard",
            event=_Event(),
            req=None,
            pending=pending,
            kind="single",
        )
        runner_task = next(task for task in plugin._active_asyncio_tasks if not task.done())
        await runner_task

        assert plugin.console_store.tasks[task_id]["status"] == "stopped"
        assert plugin.console_store.events[-1]["event_type"] == "stopped"

    asyncio.run(scenario())


def test_stop_exception_converges_batch_item_to_stopped(monkeypatch) -> None:
    async def stopped_dispatch(**kwargs):
        kwargs["event"].set_extra("agent_stop_requested", True)
        raise RuntimeError("runner stopped before final response")

    monkeypatch.setattr(main_module, "dispatch_to_maid_agent", stopped_dispatch)

    class _SessionStore:
        config = MaidModeConfig()

        async def close_session(self, _session_id: str, status: str):
            assert status == "stopped"

    async def scenario() -> None:
        plugin = _make_plugin()
        plugin.context = SimpleNamespace()
        plugin.session_store = _SessionStore()
        event = _Event()
        batch = await plugin.batch_registry.create_batch(
            batch_id="batch-1",
            unified_msg_origin=event.unified_msg_origin,
            sender_id=event.get_sender_id(),
            maid_full_reply="",
            true_user_input=None,
            image_urls_raw=None,
            session_done_requested=False,
            items=[{"agent_name": "butler", "maid_request": "stoppable item"}],
        )
        item = batch.items[0]

        await plugin._run_maid_batch_item_background_task(
            event=event,
            batch_id=batch.batch_id,
            item_id=item.item_id,
            session_id=item.session_id,
            maid_full_reply="",
            agent_name=item.agent_name,
            maid_request=item.maid_request,
            true_user_input=None,
            image_urls_raw=None,
            parent_message_id="message-1",
        )

        refreshed = await plugin.batch_registry.get_batch(batch.batch_id)
        assert refreshed is not None
        assert refreshed.items[0].status == "stopped"
        assert plugin.console_store.events[-1]["event_type"] == "stopped"

    asyncio.run(scenario())
