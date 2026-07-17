"""Tests for 1.3.0 call_maid / maid_task interfaces (mocked orchestrator)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from astrbot_plugin_maid_agent import main as main_module
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.main import (
    MaidAgent,
    _DashboardMaidEvent,
    _RuntimeTraceHooks,
)
from astrbot_plugin_maid_agent.notification_outbox import NOTIFICATION_IDS_META_KEY
from astrbot_plugin_maid_agent.runtime_orchestrator import (
    BatchCapacityError,
    CapacityExceededError,
    DispatchOutcome,
    RunNotFoundError,
)

import astrbot.api.message_components as Comp
from astrbot.api.event import MessageChain
from astrbot.core.agent.message import Message


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


def test_child_event_can_forward_messages_without_sharing_state():
    original = _DashboardMaidEvent(
        unified_msg_origin="platform:GroupMessage:group",
        sender_id="owner",
        message_text="hello",
    )
    plugin = object.__new__(MaidAgent)

    child = plugin._isolate_child_event(original, forward_sends=True)
    child.set_extra("child", True)

    asyncio.run(child.send(MessageChain(chain=[Comp.Plain("forwarded")])))

    assert original.get_extra("child") is None
    assert original.sent_messages == ["forwarded"]


def test_persist_runner_step_appends_only_new_messages():
    async def scenario():
        appended = []
        published = []

        class _Store:
            async def append_message(self, agent_id, message):
                appended.append((agent_id, message))

        plugin = object.__new__(MaidAgent)
        plugin.runtime_store = _Store()

        async def publish(run):
            published.append(run.task_id)

        plugin._publish_runtime_trace_safe = publish
        run = SimpleNamespace(agent_id="a" * 32, task_id="1" * 32)
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
        assert published == ["1" * 32]
        count = await plugin._persist_runner_step(run, runner, count)
        assert count == 2
        assert len(appended) == 1
        assert published == ["1" * 32]

    asyncio.run(scenario())


def test_runtime_tool_controls_render_live_and_historical_trace():
    records = [
        {"role": "user", "content": "do it"},
        {
            "_control": True,
            "kind": "tool_start",
            "tool_call_id": "live-1",
            "tool_name": "search",
            "arguments": {"query": "AstrBot"},
        },
    ]
    running = MaidAgent._build_runtime_tool_chain_payload(records)
    assert [entry["kind"] for entry in running["entries"]] == ["tool_call"]
    assert running["entries"][0]["tool_name"] == "search"

    records.extend(
        [
            {
                "_control": True,
                "kind": "tool_end",
                "tool_call_id": "live-1",
                "tool_name": "search",
                "result": "found",
            },
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "provider-call-1",
                        "function": {"name": "search", "arguments": '{"query":"AstrBot"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "provider-call-1", "content": "found"},
        ]
    )
    completed = MaidAgent._build_runtime_tool_chain_payload(records)
    assert [entry["kind"] for entry in completed["entries"]] == [
        "tool_call",
        "tool_result",
        "assistant",
    ]
    assert completed["entries"][1]["message"] == "found"


def test_legacy_runtime_transcript_without_controls_still_builds_history():
    payload = MaidAgent._build_runtime_tool_chain_payload(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": "read", "arguments": '{"path":"a.txt"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "text"},
        ]
    )
    assert [entry["kind"] for entry in payload["entries"]] == [
        "tool_call",
        "tool_result",
    ]


def test_runtime_trace_hooks_publish_tool_start_and_end():
    async def scenario():
        controls = []
        publishes = []

        class _Store:
            async def append_control(self, agent_id, kind, payload):
                controls.append((agent_id, kind, payload))

        async def publish():
            publishes.append(True)

        run = SimpleNamespace(agent_id="a" * 32, task_id="1" * 32)
        hooks = _RuntimeTraceHooks(run, _Store(), publish)
        tool = SimpleNamespace(name="search")
        await hooks.on_tool_start(None, tool, {"query": "x"})
        await hooks.on_tool_end(
            None,
            tool,
            {"query": "x"},
            SimpleNamespace(content=[SimpleNamespace(text="ok")], isError=False),
        )

        assert [item[1] for item in controls] == ["tool_start", "tool_end"]
        assert controls[0][2]["tool_call_id"] == controls[1][2]["tool_call_id"]
        assert controls[1][2]["result"] == "ok"
        assert len(publishes) == 2

    asyncio.run(scenario())


def test_runtime_trace_hooks_close_missing_tool_end():
    async def scenario():
        controls = []

        class _Store:
            async def append_control(self, _agent_id, kind, payload):
                controls.append((kind, payload))

        async def publish():
            return None

        run = SimpleNamespace(agent_id="a" * 32, task_id="1" * 32)
        hooks = _RuntimeTraceHooks(run, _Store(), publish)
        await hooks.on_tool_start(None, SimpleNamespace(name="broken"), {})
        await hooks.close_unfinished_tool()
        await hooks.close_unfinished_tool()

        assert [item[0] for item in controls] == ["tool_start", "tool_end"]
        assert controls[1][1]["result"].startswith("error:")

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


def test_runtime_terminal_only_triggers_outbox_delivery():
    async def scenario():
        calls = []
        plugin = object.__new__(MaidAgent)

        async def notify(_umo):
            calls.append("notify")

        plugin.outbox = SimpleNamespace(queue_delivery=notify)
        run = SimpleNamespace(
            task_id="1" * 32,
            agent_id="a" * 32,
            unified_msg_origin="umo:a:1",
            sender_id="owner",
            agent_name="butler",
            status="completed",
            mode="background",
            background_reason="timeout",
            request_text="finish",
            result="done",
            error="",
            notification=SimpleNamespace(notification_id="n" * 32),
        )

        await plugin._on_runtime_terminal(run)
        assert calls == ["notify"]

    asyncio.run(scenario())


def test_runtime_child_runner_preserves_begin_dialogs_and_compress_provider(monkeypatch):
    async def scenario():
        from astrbot_plugin_maid_agent import maid_dispatcher

        captured = {}
        compress_provider = object()
        provider = object()
        handoff = SimpleNamespace(
            provider_id="provider",
            agent=SimpleNamespace(
                instructions="instructions",
                begin_dialogs=[Message(role="user", content="seed")],
            ),
        )

        class _Context:
            async def get_current_chat_provider_id(self, _umo):
                return "provider"

            def get_provider_by_id(self, provider_id):
                return compress_provider if provider_id == "compress" else provider

            def get_config(self, **_kwargs):
                return {
                    "provider_settings": {
                        "llm_compress_provider_id": "compress",
                        "context_limit_reached_strategy": "llm_compress",
                    }
                }

        class _Runner:
            run_context = SimpleNamespace(messages=[])
            req = None

            def done(self):
                return True

            def get_final_llm_resp(self):
                return SimpleNamespace(completion_text="done")

            def follow_up(self, *, message_text):
                return SimpleNamespace(seq=1, text=message_text)

            def request_stop(self):
                return None

        async def build_runner(**kwargs):
            captured.update(kwargs)
            return _Runner()

        monkeypatch.setattr(maid_dispatcher, "_build_runner", build_runner)
        monkeypatch.setattr(
            main_module,
            "build_child_toolset",
            lambda *_args, **_kwargs: None,
        )

        async def collect_images(_event, _raw):
            return []

        monkeypatch.setattr(main_module, "collect_child_image_urls", collect_images)
        plugin = object.__new__(MaidAgent)
        plugin.context = _Context()
        plugin.maid_mode_config = MaidModeConfig(
            dispatch_prompt_template="{maid_request_block}",
        )
        plugin._resolve_handoff_for_runtime = lambda _name: (handoff, "butler")
        plugin._isolate_child_event = lambda _event: SimpleNamespace(
            get_extra=lambda _key, _default=None: None,
            set_extra=lambda _key, _value: None,
            cleanup_temporary_local_files=lambda: None,
        )
        plugin._load_provider_settings = lambda _umo: {
            "context_limit_reached_strategy": "llm_compress",
            "llm_compress_provider_id": "compress",
        }
        plugin._agent_memory_enabled = lambda _name: False
        plugin._ensure_provider_max_context_tokens = lambda _provider: 0
        plugin.runtime_store = SimpleNamespace(
            append_control=lambda *_args, **_kwargs: asyncio.sleep(0),
            append_message=lambda *_args, **_kwargs: asyncio.sleep(0),
        )
        plugin.orchestrator = SimpleNamespace(
            register_steer_handler=lambda *_args: None,
            unregister_steer_handler=lambda *_args: None,
            register_stop_handler=lambda *_args: None,
        )
        run = SimpleNamespace(
            agent_id="a" * 32,
            task_id="1" * 32,
            agent_name="butler",
            unified_msg_origin="umo:a:1",
            request_text="do it",
            resume_of="",
        )
        event = SimpleNamespace(unified_msg_origin="umo:a:1")

        child_runner = await plugin._make_child_runner(run, event, {})
        assert await child_runner.run() == "done"
        assert captured["contexts"] == handoff.agent.begin_dialogs
        assert captured["llm_compress_provider"] is compress_provider

    asyncio.run(scenario())
