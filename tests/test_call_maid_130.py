"""Tests for 1.3.0 call_maid / maid_task interfaces (mocked orchestrator)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.main import MaidAgent, _DashboardMaidEvent
from astrbot_plugin_maid_agent.notification_outbox import NOTIFICATION_IDS_META_KEY
from astrbot_plugin_maid_agent.runtime_orchestrator import (
    BatchCapacityError,
    CapacityExceededError,
    DispatchOutcome,
    RunNotFoundError,
)


class _Event:
    def __init__(self, sender_id="owner", umo="aiocqhttp:GroupMessage:g1"):
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(message_id="m1", image_urls=None)
        self._sender_id = sender_id
        self._extras: dict = {}

    def get_sender_id(self):
        return self._sender_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class _MockOrchestrator:
    """Records calls and returns scripted outcomes."""

    def __init__(self):
        self.single_calls: list[dict] = []
        self.batch_calls: list[dict] = []
        self.steer_calls: list[dict] = []
        self.stop_calls: list[dict] = []
        self.result_calls: list[dict] = []
        self._single_outcome = None
        self._batch_outcome = None
        self._steer_outcome = "steered"
        self._stop_outcome = None
        self._result_outcome = None
        self._single_exc = None
        self._batch_exc = None
        self._steer_exc = None
        self._stop_exc = None
        self._result_exc = None

    def set_single(self, outcome=None, exc=None):
        self._single_outcome = outcome
        self._single_exc = exc

    def set_batch(self, outcome=None, exc=None):
        self._batch_outcome = outcome
        self._batch_exc = exc

    def set_steer(self, outcome="steered", exc=None):
        self._steer_outcome = outcome
        self._steer_exc = exc

    def set_stop(self, outcome=None, exc=None):
        self._stop_outcome = outcome
        self._stop_exc = exc

    def set_result(self, outcome=None, exc=None):
        self._result_outcome = outcome
        self._result_exc = exc

    async def dispatch_single(self, *, event, request, runner_payload=None):
        self.single_calls.append(
            {"event": event, "request": request, "runner_payload": runner_payload}
        )
        if self._single_exc:
            raise self._single_exc
        return self._single_outcome

    async def dispatch_batch(self, *, event, requests, runner_payload=None):
        self.batch_calls.append(
            {"event": event, "requests": requests, "runner_payload": runner_payload}
        )
        if self._batch_exc:
            raise self._batch_exc
        return self._batch_outcome

    async def steer(self, *, agent_id, message_text, event=None, sender_id=""):
        self.steer_calls.append(
            {
                "agent_id": agent_id,
                "message_text": message_text,
                "event": event,
                "sender_id": sender_id,
            }
        )
        if self._steer_exc:
            raise self._steer_exc
        return self._steer_outcome

    async def stop(self, *, task_id, event=None, sender_id="", source="chat"):
        self.stop_calls.append(
            {
                "task_id": task_id,
                "event": event,
                "sender_id": sender_id,
                "source": source,
            }
        )
        if self._stop_exc:
            raise self._stop_exc
        return self._stop_outcome

    async def get_result(
        self,
        *,
        task_id,
        agent_id="",
        event=None,
        block=True,
        timeout_ms=30000,
    ):
        self.result_calls.append(
            {
                "agent_id": agent_id,
                "task_id": task_id,
                "event": event,
                "block": block,
                "timeout_ms": timeout_ms,
            }
        )
        if self._result_exc:
            raise self._result_exc
        return self._result_outcome

    async def list_active_runs(self, _unified_msg_origin, _sender_id=""):
        return []


class _MockOutbox:
    def __init__(self):
        self.claimed: list[tuple] = []

    async def note_result_claimed(self, agent_id, task_id):
        self.claimed.append((agent_id, task_id))


def _make_plugin():
    plugin = object.__new__(MaidAgent)
    plugin.maid_mode_config = MaidModeConfig()
    plugin.orchestrator = _MockOrchestrator()
    plugin.outbox = _MockOutbox()
    plugin.runtime_store = SimpleNamespace(
        list_agent_ids=_async_return([]),
        list_runs=_async_return([]),
        load_run=_async_return(None),
    )
    plugin.console_store = SimpleNamespace(ensure_task=_async_return({}))
    return plugin


def _async_return(value):
    async def _f(*_a, **_kw):
        return value

    return _f


def _outcome(**kw):
    return DispatchOutcome(
        agent_id=kw.get("agent_id", "a" * 32),
        task_id=kw.get("task_id", "1" * 32),
        agent_name=kw.get("agent_name", "butler"),
        status=kw.get("status", "completed"),
        mode=kw.get("mode", "foreground"),
        background_reason=kw.get("background_reason", ""),
        result=kw.get("result", "done"),
        error=kw.get("error", ""),
        output_file=kw.get("output_file", ""),
    )


def _parse(result_text: str) -> dict:
    return json.loads(result_text)


async def _foreground_returns_result_inline():
    plugin = _make_plugin()
    plugin.orchestrator.set_single(_outcome(status="completed", mode="foreground", result="hello"))
    res = await plugin.call_maid(_Event(), request_text="do thing", agent_name="butler")
    payload = _parse(res)
    assert payload["status"] == "completed"
    assert payload["mode"] == "foreground"
    assert payload["result"] == "hello"
    assert len(plugin.orchestrator.single_calls) == 1


def test_foreground_returns_result_inline():
    asyncio.run(_foreground_returns_result_inline())


async def _foreground_timeout_returns_background_handle():
    plugin = _make_plugin()
    plugin.orchestrator.set_single(
        _outcome(status="running", mode="background", background_reason="timeout")
    )
    res = await plugin.call_maid(_Event(), request_text="slow")
    payload = _parse(res)
    assert payload["status"] == "running"
    assert payload["mode"] == "background"
    assert payload["background_reason"] == "timeout"


def test_foreground_timeout_returns_background_handle():
    asyncio.run(_foreground_timeout_returns_background_handle())


async def _explicit_background_returns_immediately():
    plugin = _make_plugin()
    plugin.orchestrator.set_single(
        _outcome(status="running", mode="background", background_reason="explicit")
    )
    res = await plugin.call_maid(
        _Event(), request_text="bg", run_in_background=True
    )
    payload = _parse(res)
    assert payload["mode"] == "background"
    assert payload["background_reason"] == "explicit"


def test_explicit_background_returns_immediately():
    asyncio.run(_explicit_background_returns_immediately())


async def _empty_request_text_errors():
    plugin = _make_plugin()
    res = await plugin.call_maid(_Event(), request_text="")
    payload = _parse(res)
    assert payload["status"] == "error"


def test_empty_request_text_errors():
    asyncio.run(_empty_request_text_errors())


async def _capacity_exceeded_returns_error():
    plugin = _make_plugin()
    plugin.orchestrator.set_single(exc=CapacityExceededError("umo full"))
    res = await plugin.call_maid(_Event(), request_text="x")
    payload = _parse(res)
    assert payload["status"] == "error"
    assert payload["mode"] == "rejected"


def test_capacity_exceeded_returns_error():
    asyncio.run(_capacity_exceeded_returns_error())


async def _batch_dispatch_in_order():
    plugin = _make_plugin()
    from astrbot_plugin_maid_agent.runtime_orchestrator import BatchOutcome

    items = [
        _outcome(task_id="1" * 32, result="r1"),
        _outcome(task_id="2" * 32, result="r2"),
    ]
    plugin.orchestrator.set_batch(BatchOutcome(batch_id="b" * 32, items=items))
    tasks = [
        {"request_text": "t1", "agent_name": "butler"},
        {"request_text": "t2", "agent_name": "butler"},
    ]
    res = await plugin.call_maid(_Event(), tasks=tasks)
    payload = _parse(res)
    assert payload["mode"] == "batch"
    assert len(payload["items"]) == 2
    assert payload["items"][0]["task_id"] == "1" * 32
    assert payload["items"][1]["task_id"] == "2" * 32
    # Order preserved.
    sent = plugin.orchestrator.batch_calls[0]["requests"]
    assert sent[0].request_text == "t1"
    assert sent[1].request_text == "t2"


def test_batch_dispatch_in_order():
    asyncio.run(_batch_dispatch_in_order())


async def _batch_capacity_rejected():
    plugin = _make_plugin()
    plugin.orchestrator.set_batch(exc=BatchCapacityError("no room"))
    res = await plugin.call_maid(_Event(), tasks=[{"request_text": "t1"}])
    payload = _parse(res)
    assert payload["status"] == "error"
    assert payload["mode"] == "rejected"


def test_batch_capacity_rejected():
    asyncio.run(_batch_capacity_rejected())


async def _batch_too_many_items():
    plugin = _make_plugin()
    tasks = [{"request_text": f"t{i}"} for i in range(6)]
    res = await plugin.call_maid(_Event(), tasks=tasks)
    payload = _parse(res)
    assert payload["status"] == "error"
    assert "最多 5" in payload["error"]


def test_batch_too_many_items():
    asyncio.run(_batch_too_many_items())


def test_batch_empty_and_resume_are_rejected():
    async def scenario():
        plugin = _make_plugin()
        empty = _parse(await plugin.call_maid(_Event(), tasks=[]))
        assert empty["status"] == "error"
        resumed = _parse(
            await plugin.call_maid(
                _Event(),
                tasks=[{"request_text": "x"}],
                resume_agent_id="a" * 32,
            )
        )
        assert resumed["status"] == "error"

    asyncio.run(scenario())


async def _legacy_action_dispatch_routes_to_new():
    plugin = _make_plugin()
    plugin.orchestrator.set_single(_outcome(status="completed", mode="foreground", result="ok"))
    res = await plugin.call_maid(_Event(), action="dispatch", request_text="legacy")
    payload = _parse(res)
    assert payload["status"] == "completed"
    assert payload["result"] == "ok"


def test_legacy_action_dispatch_routes_to_new():
    asyncio.run(_legacy_action_dispatch_routes_to_new())


async def _legacy_action_done_is_stateless_noop():
    plugin = _make_plugin()
    # intruder event; no active task, no session -> still allowed (no-op), but
    # when an active task belongs to another sender it must be refused.
    from astrbot_plugin_maid_agent.background_registry import MaidBackgroundTaskRegistry

    plugin.background_tasks = MaidBackgroundTaskRegistry()
    await plugin.background_tasks.create_task(
        unified_msg_origin="aiocqhttp:GroupMessage:g1",
        sender_id="owner",
        agent_name="butler",
        maid_request="x",
    )
    intruder = _Event(sender_id="intruder")
    res = await plugin.call_maid(intruder, action="done")
    assert "无需显式结束" in res


def test_legacy_action_done_is_stateless_noop():
    asyncio.run(_legacy_action_done_is_stateless_noop())


async def _maid_task_result_claims_notification():
    plugin = _make_plugin()
    plugin.orchestrator.set_result(
        _outcome(status="completed", mode="background", result="answer")
    )
    res = await plugin.maid_task(
        _Event(), action="result", task_id="1" * 32, agent_id="a" * 32, block=True
    )
    payload = _parse(res)
    assert payload["status"] == "completed"
    assert payload["result"] == "answer"
    # Notification claimed.
    assert plugin.outbox.claimed == [("a" * 32, "1" * 32)]


def test_maid_task_result_claims_notification():
    asyncio.run(_maid_task_result_claims_notification())


async def _maid_task_result_nonblocking_not_ready():
    plugin = _make_plugin()
    plugin.orchestrator.set_result(
        _outcome(status="running", mode="background")
    )
    res = await plugin.maid_task(
        _Event(), action="status", task_id="1" * 32, agent_id="a" * 32
    )
    payload = _parse(res)
    assert payload["status"] == "running"
    # Non-blocking status does NOT claim.
    assert plugin.outbox.claimed == []


def test_maid_task_result_nonblocking_not_ready():
    asyncio.run(_maid_task_result_nonblocking_not_ready())


async def _maid_task_stop_routes_to_orchestrator():
    plugin = _make_plugin()
    plugin.orchestrator.set_stop(_outcome(status="stopped", mode="background"))
    res = await plugin.maid_task(_Event(), action="stop", task_id="1" * 32)
    payload = _parse(res)
    assert payload["status"] == "stopped"
    assert plugin.orchestrator.stop_calls[0]["task_id"] == "1" * 32
    assert plugin.orchestrator.stop_calls[0]["event"].get_sender_id() == "owner"


def test_maid_task_stop_routes_to_orchestrator():
    asyncio.run(_maid_task_stop_routes_to_orchestrator())


async def _maid_task_steer_routes_to_orchestrator():
    plugin = _make_plugin()
    plugin.orchestrator.set_steer(outcome="steered:more")
    res = await plugin.maid_task(
        _Event(), action="steer", agent_id="a" * 32, message="more"
    )
    payload = _parse(res)
    assert payload["status"] == "steered"
    assert plugin.orchestrator.steer_calls[0]["message_text"] == "more"


def test_maid_task_steer_routes_to_orchestrator():
    asyncio.run(_maid_task_steer_routes_to_orchestrator())


async def _maid_task_invalid_action():
    plugin = _make_plugin()
    res = await plugin.maid_task(_Event(), action="bogus")
    payload = _parse(res)
    assert payload["status"] == "error"


def test_maid_task_invalid_action():
    asyncio.run(_maid_task_invalid_action())


async def _maid_task_result_missing_ids():
    plugin = _make_plugin()
    res = await plugin.maid_task(_Event(), action="result")
    payload = _parse(res)
    assert payload["status"] == "error"


def test_maid_task_result_missing_ids():
    asyncio.run(_maid_task_result_missing_ids())


async def _maid_task_result_run_not_found():
    plugin = _make_plugin()
    plugin.orchestrator.set_result(exc=RunNotFoundError("nope"))
    res = await plugin.maid_task(
        _Event(), action="result", task_id="1" * 32, agent_id="a" * 32
    )
    payload = _parse(res)
    assert payload["status"] == "error"
    assert "nope" in payload["error"]


def test_maid_task_result_run_not_found():
    asyncio.run(_maid_task_result_run_not_found())


def test_runtime_tool_schema_has_nested_batch_items():
    call_tool = SimpleNamespace(parameters={})
    task_tool = SimpleNamespace(parameters={})

    class _Manager:
        def get_func(self, name):
            return {"call_maid": call_tool, "maid_task": task_tool}.get(name)

    plugin = object.__new__(MaidAgent)
    plugin.context = SimpleNamespace(get_llm_tool_manager=lambda: _Manager())
    plugin._patch_runtime_tool_schemas()

    item_schema = call_tool.parameters["properties"]["tasks"]["items"]
    assert item_schema["type"] == "object"
    assert item_schema["required"] == ["request_text"]
    assert item_schema["properties"]["run_in_background"]["type"] == "boolean"
    assert task_tool.parameters["properties"]["timeout_ms"]["maximum"] == 600000


def test_child_event_copies_identity_without_sharing_state():
    original = _DashboardMaidEvent(
        unified_msg_origin="platform:GroupMessage:group",
        sender_id="owner",
        message_text="hello",
    )
    original.role = "member"
    original.set_extra("parent", "value")
    plugin = object.__new__(MaidAgent)

    child = plugin._isolate_child_event(original)

    assert child.unified_msg_origin == original.unified_msg_origin
    assert child.get_sender_id() == original.get_sender_id()
    assert child.get_group_id() == original.get_group_id()
    assert child.get_platform_id() == original.get_platform_id()
    assert child.get_platform_name() == original.get_platform_name()
    assert child.role == "member"
    assert child.get_extra("parent") is None
    child.set_extra("child", True)
    assert original.get_extra("child") is None


def test_persist_runner_step_appends_only_new_messages():
    async def scenario():
        appended = []

        class _Store:
            async def append_message(self, agent_id, message):
                appended.append((agent_id, message))

        plugin = object.__new__(MaidAgent)
        plugin.runtime_store = _Store()
        run = SimpleNamespace(agent_id="a" * 32)
        runner = SimpleNamespace(
            run_context=SimpleNamespace(
                messages=[
                    {"role": "user", "content": "existing"},
                    {"role": "assistant", "content": "new"},
                ]
            )
        )
        count = await plugin._persist_runner_step(run, runner, 1)
        assert count == 2
        assert appended == [("a" * 32, {"role": "assistant", "content": "new"})]
        count = await plugin._persist_runner_step(run, runner, count)
        assert count == 2
        assert len(appended) == 1

    asyncio.run(scenario())


def test_notification_wake_preserves_send_toolset():
    async def scenario():
        plugin = object.__new__(MaidAgent)
        plugin.maid_mode_config = MaidModeConfig()
        event = _Event()
        event.set_extra(NOTIFICATION_IDS_META_KEY, ["n1"])
        sentinel = object()
        req = SimpleNamespace(func_tool=sentinel)
        await plugin.sanitize_main_model_request(event, req)
        assert req.func_tool is sentinel

    asyncio.run(scenario())
