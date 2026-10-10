"""通知唤醒主 Agent 的请求构造：转述指令落正文，不注入 system_prompt。"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from astrbot_plugin_maid_agent.config import DEFAULT_DISPATCH_PROMPT_TEMPLATE
from astrbot_plugin_maid_agent.harness.chat_dispatch import ChatRuntime
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore
from astrbot_plugin_maid_agent.main import MaidAgent

UMO = "aiocqhttp:GroupMessage:777"


class _Hub:
    def publish(self, *_args, **_kwargs):
        pass


class _Config:
    show_maid_speech = True
    show_maid_tool_status = True
    max_active_per_umo = 5
    max_active_global = 20
    memory_agent_names = ()
    retention_days = 30
    max_turn_seconds = 1800
    max_agent_steps = 128
    allowed_agent_names = ("butler",)
    default_agent_name = "butler"
    dispatch_prompt_template = DEFAULT_DISPATCH_PROMPT_TEMPLATE
    include_raw_user_input = False


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data")


@pytest.fixture()
def registry(store: SessionStore) -> DriverRegistry:
    return DriverRegistry(context=None, store=store, mux_hub=_Hub(), host_hub=_Hub(), config=_Config())


class _Conversation:
    cid = "cid-1"
    history = "[]"


class _ConversationManager:
    def __init__(self):
        self.conv = _Conversation()
        self.saved: list[tuple[str, str, list]] = []

    async def get_curr_conversation_id(self, _umo):
        return self.conv.cid

    async def new_conversation(self, _umo):
        return self.conv.cid

    async def get_conversation(self, _umo, cid):
        return self.conv if cid == self.conv.cid else None

    async def update_conversation(self, umo, cid, history=None):
        self.saved.append((umo, cid, history))
        self.conv.history = json.dumps(history)


class _ToolManager:
    def get_builtin_tool(self, _cls):
        return None


class _Ctx:
    def __init__(self):
        self.conversation_manager = _ConversationManager()
        self.sent: list[tuple[str, str]] = []
        self.config_umo: str | None = None

    def get_llm_tool_manager(self):
        return _ToolManager()

    def get_config(self, umo=None):
        self.config_umo = umo
        return {
            "provider_settings": {"streaming_response": False},
            "agent_runner": {
                "config": {
                    "misc": {"tool_call_timeout": 60, "tool_schema_mode": "full"},
                    "compression": {},
                }
            },
        }

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain.get_plain_text()))


class _Runner:
    def __init__(self, text: str):
        self._text = text

    async def step_until_done(self, _limit):
        return
        yield  # 函数体带 yield 才是异步生成器，与真实 runner 用法一致

    def get_final_llm_resp(self):
        return types.SimpleNamespace(completion_text=self._text)


def test_notify_relay_instruction_lives_in_prompt_not_system_prompt(registry, store, monkeypatch):
    """转述指令是一次性任务指令，只能写进 req.prompt；system_prompt 是
    人格区，由宿主 _ensure_persona_and_skills 拼接，插件不得注入。"""
    captured: dict[str, object] = {}

    class _BuildConfig:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def _fake_build_main_agent(*, event, plugin_context, config, req):
        captured["req"] = req
        captured["event_message"] = event.message_str
        return types.SimpleNamespace(provider_request=req, agent_runner=_Runner("端口检查完成，一切正常。"))

    # astr_main_agent / message_tools 只在作者宿主构建里可导入；
    # 注入假件后本测试在任何环境都能驱动 _notify_main_agent 的完整路径。
    fake_ama = types.ModuleType("astrbot.core.astr_main_agent")
    fake_ama.MainAgentBuildConfig = _BuildConfig
    fake_ama.build_main_agent = _fake_build_main_agent
    fake_message_tools = types.ModuleType("astrbot.core.tools.message_tools")
    fake_message_tools.SendMessageToUserTool = type("SendMessageToUserTool", (), {})
    monkeypatch.setitem(sys.modules, "astrbot.core.astr_main_agent", fake_ama)
    monkeypatch.setitem(sys.modules, "astrbot.core.tools.message_tools", fake_message_tools)

    agent = object.__new__(MaidAgent)
    agent.context = _Ctx()
    agent.store = store
    agent.registry = registry
    agent.maid_mode_config = registry.config
    agent.chat_runtime = ChatRuntime(agent)

    log = store.create_session(
        agent_preset="butler",
        meta={
            "umo": UMO,
            "agentName": "butler",
            "sourceKind": "chat",
            "notify": True,
        },
    )
    driver = registry.attach(log.session_id)
    driver.umo = UMO

    # 报告按 task 与执行轮次记账，通知必须携带这两个标识。
    state = registry.chats.reset(UMO, "cid-1")
    task = registry.tasks.create(
        scope=registry.chats.key(UMO), epoch=state["epoch"], branch="branch", request="检查端口"
    )
    execution = registry.tasks.start_round(task["taskId"], log.session_id, "检查端口")
    result = {
        "status": "completed",
        "result": "端口检查通过，无异常",
        "error": "",
        "task_id": task["taskId"],
        "round_id": execution["roundId"],
        "epoch": state["epoch"],
        "conversation_id": "cid-1",
        "description": task["description"],
    }

    async def scenario():
        return await agent._notify_main_agent(driver, result)

    assert asyncio.run(scenario()) is True

    req = captured["req"]
    # 1) 插件不再写 system_prompt，人格拼接完全交给宿主
    assert not getattr(req, "system_prompt", None)
    # 2) 指令与通知正文都在 prompt 里，指令在前
    assert "转述" in req.prompt
    assert req.prompt.index("转述") < req.prompt.index("[管家任务通知]")
    assert task["taskId"] in req.prompt
    assert execution["roundId"] in req.prompt
    assert "completed" in req.prompt
    assert "端口检查通过，无异常" in req.prompt
    # 3) 事件消息保持纯通知，指令不外溢到 extras / 事件面
    assert captured["event_message"].startswith("[管家任务通知]")
    assert task["taskId"] in captured["event_message"]
    assert "端口检查通过，无异常" in captured["event_message"]
    # 4) 模型正文兜底投递不受影响
    assert agent.context.sent == [(UMO, "端口检查完成，一切正常。")]
    # 5) 落历史的只有通知与转述结果，指令不落历史
    history = json.loads(agent.context.conversation_manager.conv.history)
    assistant_turn = history[-1]
    assert "[管家任务通知]" in assistant_turn["content"]
    assert "端口检查完成，一切正常。" in assistant_turn["content"]
    assert "转述" not in assistant_turn["content"]


@pytest.fixture()
def notification(registry, store, monkeypatch):
    agent = object.__new__(MaidAgent)
    agent.context = _Ctx()
    agent.store = store
    agent.registry = registry
    agent.maid_mode_config = registry.config
    agent.chat_runtime = ChatRuntime(agent)
    log = store.create_session(agent_preset="butler", meta={"umo": UMO, "agentName": "butler", "sourceKind": "chat"})
    driver = registry.attach(log.session_id)
    state = registry.chats.reset(UMO, "cid-1")
    task = registry.tasks.create(scope=registry.chats.key(UMO), epoch=state["epoch"], branch="branch", request="任务")
    execution = registry.tasks.start_round(task["taskId"], log.session_id, "任务")
    registry.tasks.finish(task["taskId"], execution["roundId"], {"status": "completed", "result": "任务报告", "error": ""})
    result = {
        "status": "completed", "result": "任务报告", "error": "",
        "task_id": task["taskId"], "round_id": execution["roundId"],
        "epoch": state["epoch"], "conversation_id": "cid-1", "description": "任务",
    }
    fake_ama = types.ModuleType("astrbot.core.astr_main_agent")
    fake_ama.MainAgentBuildConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)

    async def build(**kwargs):
        return types.SimpleNamespace(provider_request=kwargs["req"], agent_runner=_Runner("已完成"))

    fake_ama.build_main_agent = build
    fake_message_tools = types.ModuleType("astrbot.core.tools.message_tools")
    fake_message_tools.SendMessageToUserTool = type("SendMessageToUserTool", (), {})
    monkeypatch.setitem(sys.modules, "astrbot.core.astr_main_agent", fake_ama)
    monkeypatch.setitem(sys.modules, "astrbot.core.tools.message_tools", fake_message_tools)
    return agent, driver, result, fake_ama


def delivery_of(agent, result):
    task = agent.registry.tasks.get(result["task_id"])
    return agent.registry.tasks.round(task, result["round_id"])["delivery"]


@pytest.mark.parametrize("failure", ["conversation", "build"])
def test_notify_early_return_releases_owned_claim(notification, failure):
    agent, driver, result, fake_ama = notification

    async def unavailable(*_args, **_kwargs):
        return None

    if failure == "conversation":
        agent.context.conversation_manager.get_conversation = unavailable
    else:
        fake_ama.build_main_agent = unavailable
    assert asyncio.run(agent._notify_main_agent(driver, result)) is False
    assert delivery_of(agent, result) == "pending"


def test_notify_cancellation_releases_claim(notification):
    agent, driver, result, fake_ama = notification

    async def run():
        entered = asyncio.Event()

        async def build(**_kwargs):
            entered.set()
            await asyncio.Event().wait()

        fake_ama.build_main_agent = build
        report = asyncio.create_task(agent._notify_main_agent(driver, result))
        await entered.wait()
        assert delivery_of(agent, result) == "claimed"
        report.cancel()
        with pytest.raises(asyncio.CancelledError):
            await report
        assert delivery_of(agent, result) == "pending"

    asyncio.run(run())


def test_unowned_delivery_claim_is_not_released(notification):
    agent, driver, result, _fake_ama = notification
    assert agent.registry.tasks.claim_delivery(result["task_id"], result["round_id"])
    assert asyncio.run(agent._notify_main_agent(driver, result)) is False
    assert delivery_of(agent, result) == "claimed"


def test_restart_recovers_only_pending_report_not_execution(notification):
    agent, _driver, result, _fake_ama = notification
    agent.registry.tasks.claim_delivery(result["task_id"], result["round_id"])

    async def run():
        resumed = DriverRegistry(agent.context, agent.store, _Hub(), _Hub(), agent.maid_mode_config)
        reports = []

        async def terminal(driver, report):
            reports.append(report)
            assert not driver.busy

        resumed.on_turn_terminal = terminal
        resumed.resume_pending_reports()
        await asyncio.gather(*list(resumed._background_tasks))
        assert len(reports) == 1
        assert reports[0]["task_id"] == result["task_id"]
        assert reports[0]["round_id"] == result["round_id"]
        assert reports[0]["result"] == "任务报告"
        task = resumed.tasks.get(result["task_id"])
        assert len(task["rounds"]) == 1
        assert task["rounds"][0]["delivery"] == "pending"
        await resumed.shutdown()

    asyncio.run(run())


def test_restart_skips_report_from_reset_epoch(notification):
    agent, _driver, result, _fake_ama = notification
    agent.registry.chats.reset(UMO, "cid-1")
    agent.registry.resume_pending_reports()
    assert delivery_of(agent, result) == "skipped"
