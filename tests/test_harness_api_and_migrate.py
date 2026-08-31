"""迁移器与 RPC 分发层测试（桩 Context，无 astrbot 依赖）。"""

from __future__ import annotations

import json
from pathlib import Path

import asyncio

import pytest


def sync(coro_fn):
    import functools

    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))

    return wrapper

from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness import rpc
from astrbot_plugin_maid_agent.harness.api import ApiProxy
from astrbot_plugin_maid_agent.harness.history import derive_surface
from astrbot_plugin_maid_agent.harness.store import SessionStore


class _StubContext:
    """DriverRegistry 只在执行 turn 时才摸真 Context；API 层仅需 subagent 列表。"""

    subagent_orchestrator = None


class _StubRegistry:
    def __init__(self, store):
        self.store = store
        self.context = _StubContext()
        self.drivers = {}

    def attach(self, session_id):
        driver = self.drivers.get(session_id)
        if driver is None:
            driver = _StubDriver(self.store, session_id)
            self.drivers[session_id] = driver
        return driver

    def driver(self, session_id):
        return self.drivers.get(session_id)

    def running_count(self):
        return 0

    def capacity_available(self, umo):
        return True

    def publish_host_frame(self, payload):
        pass

    def publish_event_frame(self, *a, **k):
        pass

    def push_projection_changes(self, *a, **k):
        pass


class _StubDriver:
    def __init__(self, store, session_id):
        self.store = store
        self.session_id = session_id
        self.umo = ""
        self.agent_name = ""
        self.sender_id = ""
        self.running = False
        self.inbox = []

    def enqueue(self, message, placement="queued", item_id=None):
        self.store.log(self.session_id).append(
            "user/message", message, source_event_seqs=[]
        )

    def append_title(self, title, source="user"):
        self.store.log(self.session_id).append(
            "session/title", {"title": title, "source": {"kind": source}}
        )

    def steer(self, text):
        return "stub-ticket"

    def update_queue_item(self, item_id, action):
        from astrbot_plugin_maid_agent.harness.rpc import RpcError

        raise RpcError("queue-item-not-found", f"队列项不存在: {item_id}", {"itemId": item_id})

    def request_stop(self):
        pass


class _Holder:
    def __init__(self):
        self.config = {
            "default_agent_name": "butler",
            "max_active_per_umo": 5,
            "retention_days": 30,
        }

    def get_config(self):
        return self.config

    def settings_schema(self):
        return {"type": "object", "properties": {}}

    def save_config(self, patch):
        self.config.update(patch)
        return self.config

    def default_agent_name(self):
        return self.config["default_agent_name"]

    def version(self):
        return "test"


@pytest.fixture()
def api(tmp_path: Path):
    store = SessionStore(tmp_path / "data")
    registry = _StubRegistry(store)
    proxy = ApiProxy(store=store, registry=registry, config_holder=_Holder())
    return proxy, store


class TestSessionsApi:
    @sync
    async def test_create_list_prompt_flow(self, api):
        proxy, store = api
        created = await proxy.dispatch("session.create", {"agentPreset": "butler", "umo": "qq:GroupMessage:1"})
        sid = created["sessionId"]
        assert created["agentPreset"] == "butler"
        assert store.exists(sid)

        items = (await proxy.dispatch("session.list", {}))["items"]
        assert items[0]["sessionId"] == sid
        assert items[0]["blank"] is True

        result = await proxy.dispatch(
            "session.prompt",
            {
                "sessionId": sid,
                "mode": "queue",
                "content": [{"type": "text", "text": "你好"}],
            },
            rpc_id="rpc-1",
        )
        assert result["accepted"] is True
        events = store.log(sid).read_events()
        user_events = [e for e in events if e["type"] == "user/message"]
        assert len(user_events) == 1
        assert user_events[0]["data"]["source"]["rpcId"] == "rpc-1"

    @sync
    async def test_prompt_image_attachment_roundtrip(self, api):
        proxy, store = api
        sid = (await proxy.dispatch("session.create", {}))["sessionId"]
        import base64

        png_b64 = base64.b64encode(b"\x89PNG-fake").decode()
        await proxy.dispatch(
            "session.prompt",
            {"sessionId": sid, "mode": "queue", "content": [{"type": "image", "mediaType": "image/png", "data": png_b64}]},
        )
        events = store.log(sid).read_events()
        image_blocks = [
            b
            for e in events
            if e["type"] == "user/message"
            for b in e["data"]["content"]
            if b["type"] == "image"
        ]
        assert image_blocks, "图片块未入日志"
        attachment_id = image_blocks[0]["attachment"]["attachmentId"]
        loaded = await proxy.dispatch("session.attachment", {"sessionId": sid, "attachmentId": attachment_id})
        assert base64.b64decode(loaded["data"]) == b"\x89PNG-fake"

    @sync
    async def test_history_tail_has_projections(self, api):
        proxy, store = api
        sid = (await proxy.dispatch("session.create", {}))["sessionId"]
        log = store.log(sid)
        log.append("turn/start", {"turn": 1})
        log.append("user/message", c.user_message([c.text_block("q")]), source_event_seqs=[])
        log.append("assistant/message", {"turn": 1, "step": 1, "message": c.assistant_message([c.text_block("a")], "p", "m"), "usage": {"inputTokens": 3, "outputTokens": 2}}, source_event_seqs=[])
        log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})
        page = await proxy.dispatch("session.history", {"sessionId": sid})
        assert page["hasMore"] is False
        assert page["projections"]["values"]["tokenUsage"]["outputTokens"] == 2
        older = await proxy.dispatch("session.history", {"sessionId": sid, "beforeSeq": 2})
        assert "projections" not in older

    @sync
    async def test_rename_and_errors(self, api):
        proxy, _ = api
        sid = (await proxy.dispatch("session.create", {}))["sessionId"]
        renamed = await proxy.dispatch("session.rename", {"sessionId": sid, "title": "  新标题  "})
        assert renamed["title"] == "新标题"
        with pytest.raises(rpc.RpcError) as excinfo:
            await proxy.dispatch("session.rename", {"sessionId": sid, "title": "   "})
        assert excinfo.value.code == "title-invalid"
        with pytest.raises(rpc.RpcError) as excinfo:
            await proxy.dispatch("session.history", {"sessionId": "f" * 32})
        assert excinfo.value.code == "session-not-found"
        with pytest.raises(rpc.RpcError) as excinfo:
            await proxy.dispatch("session.list", {})
            await proxy.dispatch("wat.method", {})
        with pytest.raises(rpc.RpcError):
            await proxy.dispatch("wat.method", {})

    @sync
    async def test_settings_update(self, api):
        proxy, _ = api
        described = await proxy.dispatch("settings.describe", {})
        assert described["namespaces"][0]["ns"] == "maid"
        updated = await proxy.dispatch(
            "settings.update", {"ns": "maid", "patch": {"retention_days": 7}}
        )
        assert updated["value"]["retention_days"] == 7
        with pytest.raises(rpc.RpcError) as excinfo:
            await proxy.dispatch("settings.update", {"ns": "other", "patch": {}})
        assert excinfo.value.code == "settings-rejected"

    @sync
    async def test_fork_seed(self, api):
        proxy, store = api
        sid = (await proxy.dispatch("session.create", {}))["sessionId"]
        log = store.log(sid)
        log.append("turn/start", {"turn": 1})
        log.append("user/message", c.user_message([c.text_block("q1")]), source_event_seqs=[])
        log.append("assistant/message", {"turn": 1, "step": 1, "message": c.assistant_message([c.text_block("a1")], "p", "m")}, source_event_seqs=[])
        log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})
        log.append("turn/start", {"turn": 2})
        log.append("user/message", c.user_message([c.text_block("q2")]), source_event_seqs=[])
        log.append("assistant/message", {"turn": 2, "step": 1, "message": c.assistant_message([c.text_block("a2")], "p", "m")}, source_event_seqs=[])
        log.append("turn/end", {"turn": 2, "reason": c.reason_completed()})
        log.append("turn/start", {"turn": 3})
        log.append("user/message", c.user_message([c.text_block("q3")]), source_event_seqs=[])
        log.append("assistant/message", {"turn": 3, "step": 1, "message": c.assistant_message([c.text_block("a3")], "p", "m")}, source_event_seqs=[])
        log.append("turn/end", {"turn": 3, "reason": c.reason_completed()})

        child = await proxy.dispatch("session.fork", {"sessionId": sid})
        child_events = store.log(child["sessionId"]).read_events()
        _texts = []
        for e in derive_surface(child_events):
            data = e["data"]
            blocks = data.get("content") or data.get("message", {}).get("content") or []
            _texts.extend(b.get("text") for b in blocks)
        assert _texts == ["q1", "a1", "q2", "a2", "q3", "a3"]

        q2_seq = next(
            e["seq"] for e in store.log(sid).read_events()
            if e["type"] == "user/message" and "q2" in str(e["data"].get("content"))
        )
        child = await proxy.dispatch("session.fork", {"sessionId": sid, "atSeq": q2_seq})
        child_events = store.log(child["sessionId"]).read_events()
        surface_texts = []
        for e in derive_surface(child_events):
            data = e["data"]
            blocks = data.get("content") or data.get("message", {}).get("content") or []
            surface_texts.extend(b.get("text") for b in blocks)
        assert surface_texts == ["q1", "a1", "q2", "a2"]
        kinds = [e["type"] for e in child_events]
        assert kinds[-1] == "session/end-seed"


class TestEnvelope:
    def test_parse_and_wrap(self):
        envelope = {
            "type": "client-request",
            "rpcId": "r1",
            "method": "session.list",
            "payload": {},
        }
        rpc_id, method, payload = rpc.parse_client_request(envelope)
        assert (rpc_id, method, payload) == ("r1", "session.list", {})
        wrapped = rpc.server_response("r1", {"ok": 1})
        assert wrapped["result"] == {"ok": True, "value": {"ok": 1}}
        err = rpc.session_not_found("x")
        wrapped_err = rpc.server_response_error("r1", err)
        assert wrapped_err["result"]["error"]["code"] == "session-not-found"

    def test_parse_rejects(self):
        with pytest.raises(rpc.RpcError):
            rpc.parse_client_request({"type": "nope"})


class TestMigrate:
    def test_migrate_legacy(self, tmp_path: Path):
        store = SessionStore(tmp_path / "new")
        legacy = tmp_path / "old" / "agents"
        agent_id = "a" * 32
        agent_dir = legacy / agent_id
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.json").write_text(
            json.dumps(
                {
                    "agent_id": agent_id,
                    "unified_msg_origin": "qq:GroupMessage:1",
                    "agent_name": "butler",
                    "sender_id": "u1",
                    "title": "旧标题",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "updated_at": "2026-01-02T00:00:00+00:00",
                    "active_task_id": "",
                }
            ),
            encoding="utf-8",
        )
        records = [
            {"_control": True, "kind": "run_start", "task_id": "t1", "ts": "2026-01-01T00:00:01+00:00"},
            {"role": "user", "content": "做点事", "task_id": "t1"},
            {
                "role": "assistant",
                "content": "我来处理",
                "tool_calls": [{"id": "tc1", "function": {"name": "search", "arguments": "{\"q\":1}"}}],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "结果"},
            {"_control": True, "kind": "run_end", "task_id": "t1", "ts": "2026-01-01T00:00:05+00:00"},
        ]
        with open(agent_dir / "transcript.jsonl", "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        (agent_dir / "runs").mkdir()
        (agent_dir / "runs" / "t1.json").write_text(
            json.dumps({"task_id": "t1", "status": "completed"}), encoding="utf-8"
        )

        from astrbot_plugin_maid_agent.migrate import migrate_legacy_agents

        report = migrate_legacy_agents(store, legacy)
        assert report["migrated"] == [agent_id]
        events = store.log(agent_id).read_events()
        kinds = [e["type"] for e in events]
        assert kinds == [
            "session/title",
            "turn/start",
            "user/message",
            "assistant/message",
            "tool/call",
            "tool/result",
            "turn/end",
        ]
        call = next(e for e in events if e["type"] == "tool/call")
        result = next(e for e in events if e["type"] == "tool/result")
        assert call["data"]["callId"] == "tc1"
        assert result["data"]["message"]["content"][0]["toolCallId"] == "tc1"
        meta = store.log(agent_id).load_meta()
        assert meta["umo"] == "qq:GroupMessage:1"

        assert migrate_legacy_agents(store, legacy)["migrated"] == []
