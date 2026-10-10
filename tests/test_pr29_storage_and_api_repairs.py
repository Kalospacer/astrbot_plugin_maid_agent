"""本轮修补的存储与控制台契约回归，不调用云模型。"""

import asyncio
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import yaml
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.api import SETTINGS_KEYS, ApiProxy
from astrbot_plugin_maid_agent.harness.context_state import ContextState
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore
from astrbot_plugin_maid_agent.harness.tasks import TaskStore, write_json
from astrbot_plugin_maid_agent.main import _ConfigHolder
from packaging.specifiers import SpecifierSet

UMO = "aiocqhttp:FriendMessage:100"


class Hub:
    def publish(self, *_args, **_kwargs):
        pass


class PluginConfig(dict):
    def save_config(self):
        self.saved = True


def make_registry(tmp_path):
    context = SimpleNamespace(get_provider_by_id=lambda _pid: None)
    registry = DriverRegistry(context, SessionStore(tmp_path), Hub(), Hub(), MaidModeConfig())
    registry.resolve_handoff = lambda _name: (SimpleNamespace(provider_id="provider"), "butler")
    return registry


def test_version_declaration_matches_required_framework_callbacks():
    root = Path(__file__).resolve().parents[1]
    supported = SpecifierSet(yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))["astrbot_version"])
    assert "4.27.2" not in supported
    assert "4.27.3" in supported
    assert "4.27.4" in supported


def test_full_settings_describe_value_can_be_saved(tmp_path):
    async def run():
        registry = make_registry(tmp_path)
        plugin = SimpleNamespace(config=PluginConfig(), maid_mode_config=registry.config, registry=registry)
        api = ApiProxy(store=registry.store, registry=registry, config_holder=_ConfigHolder(plugin))
        described = (await api.settings_describe({}))["namespaces"][0]["value"]
        assert set(asdict(MaidModeConfig())) <= SETTINGS_KEYS
        described.update(max_agent_steps=96, session_idle_timeout_hours=0.5, show_maid_speech=True)
        saved = await api.settings_update({"ns": "maid", "patch": described})
        assert saved["value"]["max_agent_steps"] == 96
        assert saved["value"]["session_idle_timeout_hours"] == 0.5
        assert plugin.config.saved
        assert plugin.maid_mode_config.show_maid_speech

    asyncio.run(run())


def test_task_queries_use_scope_index_and_active_status(tmp_path, monkeypatch):
    tasks = TaskStore(tmp_path)
    for _ in range(20):
        tasks.create(scope="other", epoch="other-epoch", branch="b", request="不相关任务")
    idle = tasks.create(scope="chat", epoch="epoch", branch="a", request="已结束")
    active = tasks.create(scope="chat", epoch="epoch", branch="b", request="正在执行")
    tasks.start_round(active["taskId"], "session", "正在执行")
    reads = []
    original = Path.read_text

    def record(path, *args, **kwargs):
        if path.parent == tasks.root:
            reads.append(path.stem)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record)
    assert len(tasks.active(scope="chat", epoch="epoch")) == 1
    assert reads == [active["taskId"]]
    reads.clear()
    assert len(tasks.list(scope="chat", epoch="epoch")) == 2
    assert set(reads) == {idle["taskId"], active["taskId"]}
    assert TaskStore(tmp_path).active(scope="chat", epoch="epoch")[0]["taskId"] == active["taskId"]


def test_followups_reference_shared_context_and_release_finished_payload(tmp_path):
    tasks = TaskStore(tmp_path)
    task = tasks.create(scope="chat", epoch="epoch", branch="b", request="任务")
    main = [{"role": "user", "content": "data:image/png;base64," + "A" * 100_000}]
    a = tasks.add_followup(task["taskId"], "补充一", main)
    b = tasks.add_followup(task["taskId"], "补充二", main)
    assert a["mainContextRef"] == b["mainContextRef"]
    assert tasks._path(task["taskId"]).stat().st_size < 2000
    assert tasks.followup_context(b) == main
    tasks.set_followup_status(task["taskId"], [a["id"]], "consumed")
    tasks.retention_prune(30, {"epoch"})
    assert len(list(tasks.contexts_dir.glob("*.json"))) == 1
    assert tasks.followup_context(tasks.pending(task["taskId"])[0]) == main
    tasks.cancel_followups(task["taskId"])
    tasks.retention_prune(30, {"epoch"})
    assert not list(tasks.contexts_dir.glob("*.json"))
    assert all("mainContextRef" not in item for item in tasks.get(task["taskId"])["followups"])
    assert tasks.followup_context({"mainContext": main}) == main


def test_retention_keeps_current_task_identity_and_removes_expired_epoch(tmp_path):
    tasks = TaskStore(tmp_path)
    live = tasks.create(scope="chat", epoch="current", branch="b", request="当前任务")
    old = tasks.create(scope="chat", epoch="reset-epoch", branch="b", request="已重置任务")
    running = tasks.create(scope="other", epoch="old", branch="b", request="活跃任务")
    tasks.start_round(running["taskId"], "session", "运行")
    for task in (live, old, tasks.get(running["taskId"])):
        task["updatedAt"] = 1
        write_json(tasks._path(task["taskId"]), task)
    tasks.retention_prune(30, {"current"})
    assert tasks.get(live["taskId"])["taskId"] == live["taskId"]
    assert tasks.get(running["taskId"])["status"] == "starting"
    assert not tasks._path(old["taskId"]).exists()
    assert not tasks.list(scope="chat", epoch="reset-epoch")


def test_fork_inherits_environment_without_enabling_chat_delivery(tmp_path):
    async def run():
        registry = make_registry(tmp_path)
        identity = {"senderId": "100", "role": "admin"}
        log = registry.store.create_session(agent_preset="butler", meta={
            "umo": UMO, "senderId": "100", "identity": identity,
            "agentName": "butler", "providerId": "chosen-provider", "sourceKind": "chat",
        })
        log.append("turn/start", {"turn": 1})
        log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})
        messages = [{"role": "user", "content": "父会话的工作记录"}]
        context = ContextState(log.dir)
        context.capture(messages, "管家")
        context.checkpoint(1)
        api = ApiProxy(store=registry.store, registry=registry, config_holder=None)
        child_id = (await api.session_fork({"sessionId": log.session_id}))["sessionId"]
        child = registry.attach(child_id)
        assert child.umo == UMO
        assert child.sender_id == "100"
        assert child.provider_id == "chosen-provider"
        assert child.log.load_meta()["identity"] == identity
        assert child.log.load_meta()["sourceKind"] == "dashboard"
        assert ContextState(child.log.dir).load()["messages"] == messages

    asyncio.run(run())


def test_orphan_turn_has_forkable_actual_context(tmp_path):
    async def run():
        registry = make_registry(tmp_path)
        log = registry.store.create_session(agent_preset="butler", meta={"umo": UMO, "agentName": "butler"})
        log.append("turn/start", {"turn": 1})
        messages = [{"role": "assistant", "content": "中断前已持久化的工作"}]
        ContextState(log.dir).capture(messages, "")
        api = ApiProxy(store=registry.store, registry=registry, config_holder=None)
        child_id = (await api.session_fork({"sessionId": log.session_id}))["sessionId"]
        assert ContextState(registry.store.log(child_id).dir).load()["messages"] == messages
        assert ContextState(log.dir).fork_state(1)["messages"] == messages

    asyncio.run(run())


def test_early_provider_failure_is_forkable(tmp_path):
    async def run():
        registry = make_registry(tmp_path)
        log = registry.store.create_session(agent_preset="butler", meta={"umo": UMO, "agentName": "butler"})
        messages = [{"role": "assistant", "content": "已有工作"}]
        ContextState(log.dir).capture(messages, "")
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("新要求")])
        driver._run_context = registry.manual_execution(driver, message)
        result = await driver.run_turn(message)
        assert result["status"] == "failed"
        api = ApiProxy(store=registry.store, registry=registry, config_holder=None)
        child_id = (await api.session_fork({"sessionId": log.session_id}))["sessionId"]
        assert ContextState(registry.store.log(child_id).dir).load()["messages"] == messages

    asyncio.run(run())


def test_cleanup_drops_expired_unbound_chat_states(tmp_path):
    async def run():
        registry = make_registry(tmp_path)
        state = registry.chats.reset(UMO, "cid")
        state["lastToolAt"] = 1
        registry.chats.save(state)
        await registry.retention_prune(30)
        assert registry.chats.get(UMO) is None

    asyncio.run(run())
