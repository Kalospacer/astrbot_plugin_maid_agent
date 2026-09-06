"""通知唤醒主 Agent 的请求构造：转述指令落正文，不注入 system_prompt。"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest
from astrbot_plugin_maid_agent.config import DEFAULT_DISPATCH_PROMPT_TEMPLATE
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
        return {}

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

    log = store.create_session(
        agent_preset="butler",
        meta={
            "umo": UMO,
            "agentName": "butler",
            "sourceKind": "chat",
            "activeTaskId": "t-1",
            "notify": True,
        },
    )
    driver = registry.attach(log.session_id)
    driver.umo = UMO

    async def scenario():
        return await agent._notify_main_agent(driver, {"status": "completed", "result": "端口检查通过，无异常"})

    assert asyncio.run(scenario()) is True

    req = captured["req"]
    # 1) 插件不再写 system_prompt，人格拼接完全交给宿主
    assert not getattr(req, "system_prompt", None)
    # 2) 指令与通知正文都在 prompt 里，指令在前
    assert "转述" in req.prompt
    assert req.prompt.index("转述") < req.prompt.index("[管家任务通知]")
    assert "t-1" in req.prompt
    assert "completed" in req.prompt
    assert "端口检查通过，无异常" in req.prompt
    # 3) 事件消息保持纯通知，指令不外溢到 extras / 事件面
    assert (
        captured["event_message"]
        == f"[管家任务通知]\n- agent_id={log.session_id} task_id=t-1 status=completed\n  端口检查通过，无异常"
    )
    # 4) 模型正文兜底投递不受影响
    assert agent.context.sent == [(UMO, "端口检查完成，一切正常。")]
    # 5) 落历史的只有通知与转述结果，指令不落历史
    history = json.loads(agent.context.conversation_manager.conv.history)
    assistant_turn = history[-1]
    assert "[管家任务通知]" in assistant_turn["content"]
    assert "端口检查完成，一切正常。" in assistant_turn["content"]
    assert "转述" not in assistant_turn["content"]
