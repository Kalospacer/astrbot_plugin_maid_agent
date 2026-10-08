"""使用假 runner 验证插件预算、真实消息保存和停止联动。"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.context_state import ContextState
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore
from astrbot_plugin_maid_agent.toolset_adapter import load_execution_settings

from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import ToolSet
from astrbot.core.config.agent_runner import AGENT_RUNNER_CONFIG_DEFAULTS
from astrbot.core.utils.active_event_registry import active_event_registry


class Hub:
    def publish(self, *_args, **_kwargs):
        pass


class Provider:
    provider_config = {"max_context_tokens": 128000}

    def get_model(self):
        return "fake-model"


class FakeRunner:
    def __init__(self, request, required_steps):
        self.req = SimpleNamespace(func_tool=ToolSet())
        self.calls = 0
        self.required_steps = required_steps
        self.stopped = False
        initial = [Message.model_validate(item) for item in request["contexts"] or []]
        self.run_context = SimpleNamespace(messages=[Message(role="system", content=request["system_prompt"]), *initial, Message(role="user", content=request["prompt"])])
        self.stats = SimpleNamespace(token_usage=None)

    def done(self):
        return self.stopped or self.calls >= self.required_steps

    def request_stop(self):
        self.stopped = True

    async def step(self):
        self.calls += 1
        self.run_context.messages.append(Message(role="assistant", content=f"第{self.calls}步"))
        if False:
            yield None

    def get_final_llm_resp(self):
        return SimpleNamespace(completion_text=f"执行了{self.calls}步")

    def follow_up(self, *, message_text):
        return SimpleNamespace(seq=1, consumed=False, resolved=asyncio.Event())


def build_registry(tmp_path, config):
    context = SimpleNamespace(
        get_config=lambda **_kwargs: {
            "provider_settings": {"streaming_response": False, "max_agent_step": 1},
            "agent_runner": {"config": deepcopy(AGENT_RUNNER_CONFIG_DEFAULTS["local"])},
        },
        get_provider_by_id=lambda _provider_id: Provider(),
    )
    registry = DriverRegistry(context, SessionStore(tmp_path), Hub(), Hub(), config)
    handoff = SimpleNamespace(provider_id="provider", agent=SimpleNamespace(begin_dialogs=None, instructions="测试管家"))
    registry.resolve_handoff = lambda _name: (handoff, "butler")
    registry.build_toolset = lambda **_kwargs: ToolSet()
    return registry


@pytest.mark.parametrize("budget,steps,status,calls", [(128, 35, "completed", 35), (3, 35, "step_limit", 4)])
def test_budget_is_plugin_owned_and_can_exceed_thirty(tmp_path, monkeypatch, budget, steps, status, calls):
    async def run():
        cfg = MaidModeConfig(max_agent_steps=budget)
        registry = build_registry(tmp_path, cfg)
        created = []

        async def build(**kwargs):
            assert kwargs["llm_compress_keep_recent_ratio"] == 0.15
            assert kwargs["tool_call_timeout"] == 120
            runner = FakeRunner(kwargs, steps)
            created.append(runner)
            return runner

        monkeypatch.setattr("astrbot_plugin_maid_agent.maid_dispatcher._build_runner", build)
        log = registry.store.create_session(agent_preset="butler", meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"})
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("工作要求")])
        driver._run_context = registry.manual_execution(driver, message)
        result = await driver.run_turn(message)
        assert result["status"] == status
        assert created[0].calls == calls
        snapshot = ContextState(driver.log.dir).load()
        assert snapshot["messages"][0]["content"] == "工作要求"
        task = registry.tasks.get(driver.current_execution["task_id"])
        assert task["rounds"][0]["status"] == status
        assert driver._child_event is None
        assert not registry.tasks.pending(task["taskId"])

    asyncio.run(run())


def test_native_reset_stop_signal_reaches_child_runner(tmp_path, monkeypatch):
    async def run():
        registry = build_registry(tmp_path, MaidModeConfig())

        async def build(**kwargs):
            runner = FakeRunner(kwargs, 50)
            active_event_registry.stop_all(kwargs["event"].unified_msg_origin)
            return runner

        monkeypatch.setattr("astrbot_plugin_maid_agent.maid_dispatcher._build_runner", build)
        log = registry.store.create_session(agent_preset="butler", meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"})
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("检查")])
        driver._run_context = registry.manual_execution(driver, message)
        result = await driver.run_turn(message)
        assert result["status"] == "stopped"
        assert not driver.delivery_allowed()
        assert driver._child_event is None

    asyncio.run(run())


def test_consumed_followup_does_not_run_twice(tmp_path):
    registry = build_registry(tmp_path, MaidModeConfig())
    log = registry.store.create_session(meta={"umo": "maid:FriendMessage:console", "agentName": "butler"})
    driver = registry.attach(log.session_id)
    driver._kick = lambda: None
    message = c.user_message([c.text_block("检查")])
    driver.current_execution = registry.manual_execution(driver, message)
    task_id = driver.current_execution["task_id"]
    driver.state = "running"
    ticket = SimpleNamespace(consumed=False, resolved=asyncio.Event())
    driver._steer_fn = lambda _text: ticket
    driver.steer("补充要求")
    assert len(registry.tasks.pending(task_id)) == 1
    ticket.consumed = True
    driver._sync_followup_consumption(ContextState(log.dir))
    assert registry.tasks.pending(task_id) == []
    driver.state = "idle"
    registry.settle_execution(driver, {"status": "completed", "result": "结果", "error": ""})
    assert len(registry.tasks.get(task_id)["rounds"]) == 1
    assert not [item for item in driver.inbox if item["placement"] == "queued"]


def test_console_followup_during_prepare_is_not_double_queued(tmp_path):
    registry = build_registry(tmp_path, MaidModeConfig())
    log = registry.store.create_session(meta={"umo": "maid:FriendMessage:console", "agentName": "butler"})
    driver = registry.attach(log.session_id)
    driver._kick = lambda: None
    message = c.user_message([c.text_block("检查")])
    driver.current_execution = registry.manual_execution(driver, message)
    driver.state = "running"
    assert driver.steer("补充") == "pending"
    assert len(registry.tasks.pending(driver.current_execution["task_id"])) == 1
    assert driver.inbox == []


def test_prepare_exception_settles_task_instead_of_staying_running(tmp_path):
    async def run():
        registry = build_registry(tmp_path, MaidModeConfig())
        done = asyncio.Event()

        async def terminal(_driver, result):
            assert result["status"] == "failed"
            done.set()

        registry.on_turn_terminal = terminal
        def fail(_name):
            raise ValueError("配置解析失败")

        registry.resolve_handoff = fail
        log = registry.store.create_session(meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"})
        driver = registry.attach(log.session_id)
        message = c.user_message([c.text_block("检查")])
        execution = registry.manual_execution(driver, message)
        driver.enqueue(message, run_context=execution)
        await asyncio.wait_for(done.wait(), timeout=2)
        assert registry.tasks.get(execution["task_id"])["status"] == "failed"
        await registry.shutdown()

    asyncio.run(run())


def test_old_session_format_is_not_recovered(tmp_path):
    registry = build_registry(tmp_path, MaidModeConfig())
    log = registry.store.create_session(meta={"agentName": "butler"})
    from astrbot_plugin_maid_agent.harness.tasks import write_json

    header = log.load_header()
    header["version"] = 0
    write_json(log.header_path, header)
    with pytest.raises(ValueError, match="旧 session"):
        registry.attach(log.session_id)


def test_budget_change_during_prepare_only_affects_next_execution(tmp_path, monkeypatch):
    async def run():
        registry = build_registry(tmp_path, MaidModeConfig(max_agent_steps=128))
        calls = []

        async def build(**kwargs):
            registry.config = MaidModeConfig(max_agent_steps=1)
            runner = FakeRunner(kwargs, 35)
            calls.append(runner)
            return runner

        monkeypatch.setattr("astrbot_plugin_maid_agent.maid_dispatcher._build_runner", build)
        log = registry.store.create_session(meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"})
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("工作")])
        driver._run_context = registry.manual_execution(driver, message)
        first = await driver.run_turn(message)
        assert first["status"] == "completed"
        assert calls[0].calls == 35
        driver._run_context = registry.manual_execution(driver, message)
        second = await driver.run_turn(message)
        assert second["status"] == "step_limit"
        assert calls[1].calls == 2

    asyncio.run(run())


def test_compression_does_not_hide_current_step_from_console(tmp_path, monkeypatch):
    class CompactRunner(FakeRunner):
        async def step(self):
            self.calls += 1
            if self.calls == 2:
                self.run_context.messages = [self.run_context.messages[0], Message(role="user", content="压缩后的摘要")]
            self.run_context.messages.append(Message(role="assistant", content=f"第{self.calls}步"))
            if False:
                yield None

    async def run():
        registry = build_registry(tmp_path, MaidModeConfig())
        async def build(**kwargs):
            return CompactRunner(kwargs, 3)

        monkeypatch.setattr("astrbot_plugin_maid_agent.maid_dispatcher._build_runner", build)
        log = registry.store.create_session(meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"})
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("工作")])
        driver._run_context = registry.manual_execution(driver, message)
        assert (await driver.run_turn(message))["status"] == "completed"
        texts = [event["data"]["message"]["content"][0]["text"] for event in log.read_events() if event["type"] == "assistant/message"]
        assert texts == ["第1步", "第2步", "第3步"]
        assert ContextState(log.dir).load()["messages"][0]["content"] == "压缩后的摘要"

    asyncio.run(run())


def test_execution_settings_support_both_config_layouts():
    """框架把压缩设置从 provider_settings 迁进 agent_runner.config 并删除旧键，
    插件必须在迁移前后两种布局下都取到执行设置。"""
    legacy = SimpleNamespace(
        get_config=lambda **_kw: {
            "provider_settings": {
                "streaming_response": True,
                "tool_call_timeout": 45,
                "tool_schema_mode": "skills_like",
                "context_limit_reached_strategy": "llm_compress",
                "llm_compress_instruction": "旧结构指令",
                "llm_compress_keep_recent_ratio": 0.2,
                "llm_compress_provider_id": "provider-legacy",
                "max_context_length": 12,
                "dequeue_context_length": 3,
                "fallback_max_context_tokens": 64000,
            }
        }
    )
    settings = load_execution_settings(legacy, "umo")
    assert settings["streaming_response"] is True
    assert settings["tool_call_timeout"] == 45
    assert settings["tool_schema_mode"] == "skills_like"
    assert settings["context_limit_reached_strategy"] == "llm_compress"
    assert settings["llm_compress_instruction"] == "旧结构指令"
    assert settings["llm_compress_keep_recent_ratio"] == 0.2
    assert settings["llm_compress_provider_id"] == "provider-legacy"
    assert settings["max_context_length"] == 12
    assert settings["dequeue_context_length"] == 3
    assert settings["fallback_max_context_tokens"] == 64000

    current = SimpleNamespace(
        get_config=lambda **_kw: {
            "provider_settings": {"streaming_response": False},
            "agent_runner": {
                "config": {
                    "misc": {"tool_call_timeout": 90, "tool_schema_mode": "full"},
                    "compression": {
                        "max_turns": 20,
                        "trim_turns": 5,
                        "overflow_strategy": "llm_compress",
                        "instruction": "新结构指令",
                        "keep_recent_ratio": 0.3,
                        "provider_id": "provider-current",
                        "fallback_max_tokens": 200000,
                    },
                }
            },
        }
    )
    settings = load_execution_settings(current, "umo")
    assert settings["streaming_response"] is False
    assert settings["tool_call_timeout"] == 90
    assert settings["tool_schema_mode"] == "full"
    assert settings["llm_compress_instruction"] == "新结构指令"
    assert settings["llm_compress_keep_recent_ratio"] == 0.3
    assert settings["llm_compress_provider_id"] == "provider-current"
    assert settings["max_context_length"] == 20
    assert settings["dequeue_context_length"] == 5
    assert settings["fallback_max_context_tokens"] == 200000


def test_execution_settings_tolerate_missing_sections():
    """配置结构缺段时回退默认值，不能因为读设置把子代理执行打挂。"""
    bare = SimpleNamespace(get_config=lambda **_kw: {})
    settings = load_execution_settings(bare, "umo")
    assert settings["max_context_length"] == -1
    assert settings["dequeue_context_length"] == 1
    assert settings["tool_call_timeout"] == 60
    assert settings["tool_schema_mode"] == "full"
    assert settings["context_limit_reached_strategy"] == "truncate_by_turns"


def test_turn_runs_on_legacy_provider_settings_layout(tmp_path, monkeypatch):
    """线上框架仍是迁移前的按键布局时，一整轮子代理执行必须能跑完。"""
    async def run():
        registry = build_registry(tmp_path, MaidModeConfig(max_agent_steps=6))
        # 只保留迁移前的键：没有 agent_runner.config，也没有框架的轮数配置。
        registry.context.get_config = lambda **_kw: {
            "provider_settings": {
                "streaming_response": False,
                "tool_call_timeout": 30,
                "tool_schema_mode": "full",
                "max_context_length": 5,
                "dequeue_context_length": 2,
            }
        }

        async def build(**kwargs):
            assert kwargs["tool_call_timeout"] == 30
            assert kwargs["tool_schema_mode"] == "full"
            assert kwargs["llm_compress_keep_recent_ratio"] == 0.15
            assert kwargs["enforce_max_turns"] == 5
            assert kwargs["truncate_turns"] == 2
            return FakeRunner(kwargs, 3)

        monkeypatch.setattr("astrbot_plugin_maid_agent.maid_dispatcher._build_runner", build)
        log = registry.store.create_session(
            meta={"umo": "maid:FriendMessage:console", "agentName": "butler", "sourceKind": "dashboard"}
        )
        driver = registry.attach(log.session_id)
        driver._kick = lambda: None
        message = c.user_message([c.text_block("检查")])
        driver._run_context = registry.manual_execution(driver, message)
        assert (await driver.run_turn(message))["status"] == "completed"

    asyncio.run(run())
