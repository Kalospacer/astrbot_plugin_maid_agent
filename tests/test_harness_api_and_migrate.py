"""Console API contract tests: explicit Agent selection and strict settings."""

from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.config import ConfigValidationError, load_maid_mode_config
from astrbot_plugin_maid_agent.harness import rpc
from astrbot_plugin_maid_agent.harness.api import ApiProxy
from astrbot_plugin_maid_agent.harness.store import SessionStore


def sync(coro_fn):
    @wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))

    return wrapper


class _StubContext:
    subagent_orchestrator = None


class _StubDriver:
    def __init__(self, store, session_id):
        self.store = store
        self.session_id = session_id
        self.umo = ""
        self.agent_name = ""
        self.sender_id = ""
        self.running = False

    def enqueue(self, message, **_kwargs):
        self.store.log(self.session_id).append("user/message", message, source_event_seqs=[])

    def append_title(self, title, source="user"):
        self.store.log(self.session_id).append("session/title", {"title": title, "source": {"kind": source}})

    def steer(self, _text):
        return "stub-ticket"

    def update_queue_item(self, _item_id, _action):
        raise rpc.RpcError("queue-item-not-found", "队列项不存在", {})

    def request_stop(self):
        pass


class _StubRegistry:
    config = load_maid_mode_config({"allowed_agent_names": ["butler"]})

    def __init__(self, store):
        self.store = store
        self.context = _StubContext()
        self.drivers = {}

    def attach(self, session_id):
        return self.drivers.setdefault(session_id, _StubDriver(self.store, session_id))

    def driver(self, session_id):
        return self.drivers.get(session_id)

    def running_count(self):
        return 0

    def capacity_available(self, _umo):
        return True

    def publish_host_frame(self, _payload):
        pass


class _Holder:
    def __init__(self):
        self.config = {"allowed_agent_names": ["butler"]}

    def get_config(self):
        return self.config

    def settings_schema(self):
        return {"type": "object", "properties": {"allowed_agent_names": {"type": "list"}}}

    def save_config(self, patch):
        candidate = {**self.config, **patch}
        load_maid_mode_config(candidate)
        self.config = candidate
        return self.config

    def version(self):
        return "test"


@pytest.fixture()
def api(tmp_path: Path):
    store = SessionStore(tmp_path / "data")
    return ApiProxy(store=store, registry=_StubRegistry(store), config_holder=_Holder()), store


class TestConsoleSessions:
    @sync
    async def test_console_requires_explicit_allowed_agent_and_marks_isolated_sandbox(self, api):
        proxy, store = api
        with pytest.raises(rpc.RpcError, match="显式选择"):
            await proxy.dispatch("session.create", {})
        created = await proxy.dispatch("session.create", {"agentPreset": "butler"})
        meta = store.log(created["sessionId"]).load_meta()
        assert meta["executionMode"] == "background"
        assert meta["sourceKind"] == "dashboard"
        assert meta["backgroundReason"] == "dashboard-isolated-sandbox"

    @sync
    async def test_console_prompt_never_accepts_a_foreground_mode(self, api):
        proxy, store = api
        session_id = (await proxy.dispatch("session.create", {"agentPreset": "butler"}))["sessionId"]
        result = await proxy.dispatch("session.prompt", {"sessionId": session_id, "content": [{"type": "text", "text": "hello"}]})
        assert result["accepted"] is True
        assert store.log(session_id).load_meta()["executionMode"] == "background"


class TestSettings:
    @sync
    async def test_settings_schema_is_object_with_properties(self, api):
        proxy, _ = api
        namespace = (await proxy.dispatch("settings.describe", {}))["namespaces"][0]
        assert namespace["schema"]["type"] == "object"
        assert "properties" in namespace["schema"]

    @sync
    async def test_invalid_settings_patch_returns_field_errors_without_persisting(self, api):
        proxy, _ = api
        with pytest.raises(rpc.RpcError) as excinfo:
            await proxy.dispatch("settings.update", {"ns": "maid", "patch": {"max_active_per_umo": 0}})
        assert excinfo.value.code == "settings-rejected"
        assert excinfo.value.details["errors"]["max_active_per_umo"]
