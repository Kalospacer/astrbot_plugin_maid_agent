"""模型工具路由的实际状态复现，不调用云模型。"""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.harness.chat_dispatch import MAIN_CONTEXT_KEY, ChatRuntime
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore

UMO = "aiocqhttp:FriendMessage:100"


class Hub:
    def publish(self, *_args, **_kwargs):
        pass


class Conversations:
    cid = "conversation"

    async def get_curr_conversation_id(self, _umo):
        return self.cid

    async def get_conversation(self, _umo, _cid):
        return SimpleNamespace(history=json.dumps([]))


class Event:
    unified_msg_origin = UMO
    role = "admin"
    message_obj = SimpleNamespace(message=[])

    def __init__(self):
        self.extras = {MAIN_CONTEXT_KEY: SimpleNamespace(messages=[{"role": "user", "content": "当前主背景"}])}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_sender_id(self):
        return "100"

    def get_sender_name(self):
        return "用户"

    def get_self_id(self):
        return "bot"

    def get_group_id(self):
        return ""


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    cfg = MaidModeConfig()
    store = SessionStore(tmp_path / "runtime_v3")
    context = SimpleNamespace(conversation_manager=Conversations())
    registry = DriverRegistry(context, store, Hub(), Hub(), cfg)
    registry.resolve_handoff = lambda _name: (None, cfg.default_agent_name)

    async def ensure(*_args):
        return False

    monkeypatch.setattr("astrbot_plugin_maid_agent.harness.chat_dispatch.ensure_default_subagent", ensure)
    plugin = SimpleNamespace(context=context, store=store, registry=registry, maid_mode_config=cfg)

    def create(umo, name, *, dispatch_id, identity):
        log = store.create_session(agent_preset=name, meta={"umo": umo, "agentName": name, "sourceKind": "chat", "identity": identity})
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        return log.session_id

    plugin._create_chat_agent = create
    plugin._agent_progress = lambda _driver: {"step": 1}
    clock = [1000]
    registry.chats.clock = lambda: clock[0]
    return ChatRuntime(plugin), Event(), clock


def finish(runtime, task_id, result="完成"):
    task = runtime.tasks.get(task_id)
    driver = runtime.registry.drivers[task["sessionId"]]
    item = driver.inbox.pop(0)
    driver.current_execution = item["run_context"]
    driver.state = "idle"
    runtime.registry.settle_execution(driver, {"status": "completed", "result": result, "error": ""})


def test_single_new_task_reuses_session_not_task_id(runtime):
    async def run():
        rt, event, _clock = runtime
        first = await rt.dispatch(event, prompt="检查数据库")
        original_session = rt.tasks.get(first["task_id"])["sessionId"]
        assert first["description"] == "检查数据库"
        finish(rt, first["task_id"])
        second = await rt.dispatch(event, prompt="检查连接池")
        assert second["task_id"] != first["task_id"]
        assert rt.tasks.get(second["task_id"])["sessionId"] == original_session

    asyncio.run(run())


def test_running_new_errors_force_new_is_independent(runtime):
    async def run():
        rt, event, _clock = runtime
        first = await rt.dispatch(event, prompt="A")
        result = await rt.dispatch(event, prompt="B")
        assert result["status"] == "error"
        assert "maid_send_message" in result["error"]
        assert result["tasks"][0]["task_id"] == first["task_id"]
        second = await rt.dispatch(event, prompt="B", force_new=True)
        assert second["status"] == "running"
        assert rt.tasks.get(second["task_id"])["sessionId"] != rt.tasks.get(first["task_id"])["sessionId"]
        assert rt.chats.get(UMO)["defaultBranch"] is None

    asyncio.run(run())


def test_batch_only_selects_default_after_all_end(runtime):
    async def run():
        rt, event, _clock = runtime
        batch = await rt.dispatch(event, tasks=[{"prompt": "数据库"}, {"prompt": "网络"}])
        a, b = [item["task_id"] for item in batch["tasks"]]
        finish(rt, a)
        resumed_a = await rt.dispatch(event, prompt="继续数据库", task_id=a)
        assert resumed_a["task_id"] == a
        assert rt.chats.get(UMO)["defaultBranch"] is None
        finish(rt, a)
        finish(rt, b)
        ambiguous = await rt.dispatch(event, prompt="继续深入")
        assert ambiguous["status"] == "error"
        chosen = await rt.dispatch(event, prompt="继续网络", task_id=b)
        assert chosen["task_id"] == b
        assert rt.chats.get(UMO)["defaultBranch"] == rt.tasks.get(b)["branchId"]
        finish(rt, b)
        following = await rt.dispatch(event, prompt="新的检查")
        assert rt.tasks.get(following["task_id"])["sessionId"] == rt.tasks.get(b)["sessionId"]
        finish(rt, following["task_id"])
        await rt.dispatch(event, tasks=[{"prompt": "C"}, {"prompt": "D"}])
        assert rt.chats.get(UMO)["defaultBranch"] is None

    asyncio.run(run())


def test_expired_session_does_not_invalidate_task(runtime):
    async def run():
        rt, event, clock = runtime
        result = await rt.dispatch(event, prompt="检查")
        task_id = result["task_id"]
        sid = rt.tasks.get(task_id)["sessionId"]
        finish(rt, task_id)
        clock[0] += 5 * 3_600_000 + 1
        continued = await rt.dispatch(event, prompt="继续检查", task_id=task_id)
        assert continued["task_id"] == task_id
        assert continued["status"] == "running"
        assert rt.tasks.get(task_id)["sessionId"] != sid

    asyncio.run(run())


def test_send_is_not_implicitly_converted_when_task_ends(runtime):
    async def run():
        rt, event, _clock = runtime
        result = await rt.dispatch(event, prompt="检查")
        task_id = result["task_id"]
        sent = await rt.send(event, task_id, "先不要修改")
        assert sent["status"] == "accepted"
        assert rt.tasks.pending(task_id)[0]["content"] == "先不要修改"
        rt.tasks.set_followup_status(task_id, [sent["followup_id"]], "consumed")
        finish(rt, task_id)
        ended = await rt.send(event, task_id, "继续")
        assert ended["status"] == "error"
        assert "maid_agent" in ended["error"]

    asyncio.run(run())


def test_unconsumed_followup_schedules_another_round_and_keeps_results(runtime):
    async def run():
        rt, event, _clock = runtime
        result = await rt.dispatch(event, prompt="检查")
        task_id = result["task_id"]
        await rt.send(event, task_id, "再检查配置")
        finish(rt, task_id, "原轮结果")
        task = rt.tasks.get(task_id)
        assert len(task["rounds"]) == 2
        assert task["rounds"][0]["result"] == "原轮结果"
        assert task["rounds"][1]["request"] == "再检查配置"
        assert task["status"] == "starting"
        assert rt.chats.get(UMO)["idleSince"] is None
        driver = rt.registry.drivers[task["sessionId"]]
        assert driver.inbox[0]["run_context"]["task_id"] == task_id

    asyncio.run(run())


def test_stop_starting_task_does_not_stick_in_running_state(runtime):
    async def run():
        rt, event, _clock = runtime
        result = await rt.dispatch(event, prompt="检查")
        await rt.send(event, result["task_id"], "追加要求")
        await rt.stop(event, result["task_id"])
        assert rt.tasks.get(result["task_id"])["status"] == "stopped"
        assert not rt.chats.active(rt.chats.get(UMO))
        assert rt.chats.get(UMO)["idleSince"] is not None

    asyncio.run(run())


def test_old_completed_task_cannot_queue_on_occupied_session(runtime):
    async def run():
        rt, event, _clock = runtime
        first = await rt.dispatch(event, prompt="A")
        finish(rt, first["task_id"])
        second = await rt.dispatch(event, prompt="B")
        continued = await rt.dispatch(event, prompt="继续A", task_id=first["task_id"])
        assert continued["status"] == "error"
        assert "不排队" in continued["error"]
        assert len(rt.tasks.get(first["task_id"])["rounds"]) == 1
        assert rt.tasks.get(second["task_id"])["status"] == "starting"

    asyncio.run(run())


def test_default_agent_change_keeps_work_session(runtime):
    async def run():
        rt, event, _clock = runtime
        first = await rt.dispatch(event, prompt="检查")
        sid = rt.tasks.get(first["task_id"])["sessionId"]
        finish(rt, first["task_id"])
        rt.plugin.maid_mode_config = MaidModeConfig(default_agent_name="new-butler", allowed_agent_names=("new-butler",))
        next_task = await rt.dispatch(event, prompt="继续工作")
        assert rt.tasks.get(next_task["task_id"])["sessionId"] == sid
        assert rt.registry.drivers[sid].agent_name == "new-butler"

    asyncio.run(run())


def test_reset_cancels_starting_work_and_old_reports(runtime):
    async def run():
        rt, event, _clock = runtime
        created = await rt.dispatch(event, prompt="检查")
        task_id = created["task_id"]
        await rt.send(event, task_id, "补充")
        previous = rt.chats.get(UMO)
        execution = deepcopy(rt.registry.drivers[rt.tasks.get(task_id)["sessionId"]].inbox[0]["run_context"])
        rt.invalidate(previous)
        rt.chats.reset(UMO, previous["conversationId"])
        assert rt.tasks.get(task_id)["status"] == "stopped"
        assert rt.tasks.get(task_id)["followups"][0]["status"] == "cancelled"
        assert not rt.allowed(execution, UMO)
        assert not rt.chats.get(UMO)["branches"]

    asyncio.run(run())


def test_context_deletion_does_not_delete_task_identity(runtime):
    async def run():
        rt, event, _clock = runtime
        created = await rt.dispatch(event, prompt="检查")
        task_id = created["task_id"]
        sid = rt.tasks.get(task_id)["sessionId"]
        finish(rt, task_id)
        rt.registry.drop_context_binding(sid)
        resumed = await rt.dispatch(event, prompt="继续", task_id=task_id)
        assert resumed["task_id"] == task_id
        assert rt.tasks.get(task_id)["sessionId"] != sid

    asyncio.run(run())


def test_main_reset_hook_uses_success_marker_not_text(runtime):
    from astrbot_plugin_maid_agent.main import MaidAgent

    async def run():
        rt, event, _clock = runtime
        created = await rt.dispatch(event, prompt="检查")
        previous = rt.chats.get(UMO)
        plugin = object.__new__(MaidAgent)
        plugin.context = rt.plugin.context
        plugin.registry = rt.registry
        plugin.chat_runtime = rt
        event.message_str = "/reset"
        await plugin.stash_raw_input(event)
        assert rt.chats.get(UMO)["epoch"] == previous["epoch"]
        assert rt.tasks.get(created["task_id"])["status"] == "starting"
        event.set_extra("_clean_group_context_session", True)
        await plugin.stash_raw_input(event)
        assert rt.chats.get(UMO)["epoch"] != previous["epoch"]
        assert rt.tasks.get(created["task_id"])["status"] == "stopped"

    asyncio.run(run())


def test_model_schema_uses_tasks_not_session_or_agent_names():
    from astrbot_plugin_maid_agent.constants import MAID_TOOL_NAMES
    from astrbot_plugin_maid_agent.main import MaidAgent

    tools = {name: SimpleNamespace(parameters={}) for name in MAID_TOOL_NAMES}
    plugin = object.__new__(MaidAgent)
    plugin.context = SimpleNamespace(get_llm_tool_manager=lambda: SimpleNamespace(get_func=lambda name: tools.get(name)))
    plugin._patch_llm_tool_schemas()
    schema = tools["maid_agent"].parameters
    assert set(schema["properties"]) == {"prompt", "task_id", "force_new", "tasks"}
    assert set(schema["properties"]["tasks"]["items"]["properties"]) == {"prompt"}
    assert set(tools["maid_send_message"].parameters["properties"]) == {"task_id", "message"}


def test_stopped_main_request_does_not_create_late_task(runtime):
    async def run():
        rt, event, _clock = runtime
        event.set_extra("agent_stop_requested", True)
        result = await rt.dispatch(event, prompt="工作")
        assert result["status"] == "error"
        assert not rt.chats.task_list(rt.chats.get(UMO))
        assert not rt.registry.drivers

    asyncio.run(run())
