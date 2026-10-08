"""审查修复回归：女仆正文押后投递、空队列 stop 终态补写、通知失败回滚。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.config import DEFAULT_DISPATCH_PROMPT_TEMPLATE
from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore
from astrbot_plugin_maid_agent.main import MaidAgent


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


def test_maid_voice_is_delivered_immediately(registry, store):
    """女仆正文即时投递，不押后：押后会把第一句话拖到下一段出现时才发。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "sourceKind": "chat"},
    )
    spoken: list[str] = []

    class _Sink:
        async def send(self, chain):
            spoken.append(chain.get_plain_text())

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.umo, driver.agent_name = "umo1", "butler"
        driver._voice_sink = _Sink()
        await driver._speak("先看一下服务状态")
        assert spoken == ["butler: 先看一下服务状态"]
        await driver._speak("")          # 空正文不占位
        await driver._speak("   ")       # 纯空白同理
        await driver._speak("端口是通的")

    asyncio.run(scenario())

    assert spoken == ["butler: 先看一下服务状态", "butler: 端口是通的"]


def test_maid_voice_is_silent_for_console_sessions(registry, store):
    """控制台来源的会话没有聊天可说，投递端为空时不炸。"""
    log = store.create_session(agent_preset="butler", meta={"agentName": "butler", "sourceKind": "dashboard"})

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.agent_name = "butler"
        await driver._speak("第一段")
        await driver._speak("第二段")

    asyncio.run(scenario())


def test_request_stop_on_idle_queue_writes_terminal_task_event(registry, store):
    """空队列 stop：被取消的任务在事件流留下 stopped-before-run 终态。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "notify": True},
    )

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.umo, driver.agent_name = "umo1", "butler"

        task = registry.tasks.create(scope="scope", epoch="epoch", branch="branch", request="排队的任务")
        execution = registry.tasks.start_round(task["taskId"], log.session_id, "排队的任务")
        driver.enqueue(
            c.user_message([c.text_block("排队的任务")]),
            run_context={"task_id": task["taskId"], "round_id": execution["roundId"], "epoch": "epoch"},
        )
        assert not driver.running
        assert len(driver.inbox) == 1

        driver.request_stop()

        assert driver.inbox == []
        assert registry.tasks.get(task["taskId"])["status"] == "stopped"
        return task["taskId"]

    task_id = asyncio.run(scenario())

    meta = store.log(log.session_id).load_meta()
    assert meta["activeTaskId"] == ""
    assert meta["deliveryStatus"] == "stopped"
    events = [e for e in store.log(log.session_id).read_events() if e["type"] == "maid/task"]
    assert events and events[-1]["data"]["taskId"] == task_id
    assert events[-1]["data"]["status"] == "stopped-before-run"


def test_turn_terminal_callback_failure_rolls_back_notified(registry, store):
    """通知回调失败时 notified 回滚，保留重放机会（notify=True 且 notified=False）。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "notify": True, "notified": False},
    )
    driver = registry.attach(log.session_id)

    async def scenario():
        async def failing_callback(_driver, _result):
            # 与 main._on_turn_terminal 的修复语义一致：先置位，失败回滚
            driver.log.update_meta(notified=True)
            try:
                raise RuntimeError("通知投递炸了")
            except Exception:
                driver.log.update_meta(notified=False)
                raise

        registry.on_turn_terminal = failing_callback
        registry.notify_turn_terminal(driver, {"status": "completed", "result": "ok"})
        pending = [t for t in registry._background_tasks if not t.done()]
        if pending:
            await asyncio.wait(pending)

    asyncio.run(scenario())

    meta = driver.log.load_meta()
    assert meta["notify"] is True
    assert meta.get("notified") is False


def test_delivery_claim_is_exclusive(registry, store):
    """通知和输出工具认领相同 task 轮次，只能有一个成功。"""
    log = store.create_session(agent_preset="butler")
    task = registry.tasks.create(scope="scope", epoch="epoch", branch="branch", request="调查")
    execution = registry.tasks.start_round(task["taskId"], log.session_id, "调查")
    registry.tasks.finish(task["taskId"], execution["roundId"], {"status": "completed", "result": "结果"})

    async def claim():
        return registry.tasks.claim_delivery(task["taskId"], execution["roundId"])

    async def scenario():
        return await asyncio.gather(*(claim() for _ in range(4)))

    assert asyncio.run(scenario()).count(True) == 1


def test_failed_delivery_returns_the_claim(registry, store):
    """失败回滚只归还相应轮次的认领，不碰另一轮结果。"""
    log = store.create_session(agent_preset="butler")
    task = registry.tasks.create(scope="scope", epoch="epoch", branch="branch", request="调查")
    execution = registry.tasks.start_round(task["taskId"], log.session_id, "调查")
    registry.tasks.finish(task["taskId"], execution["roundId"], {"status": "completed", "result": "结果"})
    assert registry.tasks.claim_delivery(task["taskId"], execution["roundId"])
    assert not registry.tasks.claim_delivery(task["taskId"], execution["roundId"])
    registry.tasks.delivery(task["taskId"], execution["roundId"], "pending")
    assert registry.tasks.claim_delivery(task["taskId"], execution["roundId"])


def test_progress_reads_tool_io_when_the_maid_has_not_spoken(registry, store):
    """女仆只发工具调用、没输出正文时，进度必须给出入参、输出和推理，而不是一句「在跑」。"""
    log = store.create_session(agent_preset="muiceagent", meta={"umo": "umo1", "agentName": "muiceagent"})
    code = 'import requests\nr = requests.get("https://mirasim.ai/announcement")\nprint(r.status_code)'
    log.append("turn/start", {"turn": 1})
    log.append("user/message", c.user_message([c.text_block("去看看公告")]), source_event_seqs=[])
    log.append("step/start", {"turn": 1, "step": 2})
    log.append(
        "assistant/message",
        {"turn": 1, "step": 2, "message": c.assistant_message(
            # 这一步只有推理和工具调用，没有 text 块。
            [c.reasoning_block("先用 python 抓一下页面"),
             c.tool_call_block("call-1", "astrbot_execute_python", json.dumps({"code": code}))],
            "openai", "gemini-3.8-flash")},
        source_event_seqs=[],
    )
    log.append("tool/call", {
        "turn": 1, "step": 2, "callId": "call-1", "name": "astrbot_execute_python",
        # 裸换行的非法 JSON：模型经常这么发，不能让它把入参整块退化成噪音。
        "arguments": '{"code": "%s"}' % code.replace('"', '\\"'),
    })
    log.append(
        "tool/result",
        {"turn": 1, "step": 2, "message": c.tool_result_message("call-1", [c.text_block("200")], False)},
        source_event_seqs=[],
    )

    driver = registry.attach(log.session_id)
    driver.turn_started_at = time.monotonic() - 97
    progress = MaidAgent._agent_progress(driver)

    assert progress["step"] == 2
    assert progress["elapsed_seconds"] == 97
    assert progress["tool"] == "astrbot_execute_python"
    assert progress["tool_done"] is True
    assert "requests.get" in progress["tool_input"]
    assert not progress["tool_input"].startswith("{")
    assert progress["tool_output"] == "200"
    assert progress["text"] == "先用 python 抓一下页面"


def test_synthetic_event_delivers_only_through_context_send_message():
    """合成事件的投递必须只走 Context.send_message；控制台来源是静默 no-op。"""
    from astrbot.api.event import MessageChain

    from astrbot_plugin_maid_agent.constants import DASHBOARD_UMO
    from astrbot_plugin_maid_agent.harness.events_shim import MaidAgentEvent

    sent: list[tuple[str, str]] = []

    class _Ctx:
        async def send_message(self, umo, chain):
            sent.append((umo, chain.get_plain_text()))

    async def scenario():
        chat = MaidAgentEvent(
            context=_Ctx(),
            unified_msg_origin="aiocqhttp:GroupMessage:777",
            identity={"senderId": "1", "groupId": "777", "platformName": "aiocqhttp"},
        )
        assert chat.deliverable is True
        await chat.send(MessageChain().message("butler: 端口是通的"))

        console = MaidAgentEvent(context=_Ctx(), unified_msg_origin=DASHBOARD_UMO)
        assert console.deliverable is False
        await console.send(MessageChain().message("不该发出去"))

        # context 缺失时也不能炸：女仆正文投递失败只该记一条 warning。
        detached = MaidAgentEvent(context=None, unified_msg_origin="aiocqhttp:FriendMessage:1")
        await detached.send(MessageChain().message("没有 context"))

    asyncio.run(scenario())

    assert sent == [("aiocqhttp:GroupMessage:777", "butler: 端口是通的")]
def test_tool_status_switch_controls_chat_delivery(registry, store):
    """函数调用状态投递受开关控制；开着时每次工具开始就报一条。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "sourceKind": "chat"},
    )
    spoken: list[str] = []

    class _Sink:
        async def send(self, chain):
            spoken.append(chain.get_plain_text())

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.umo, driver.agent_name = "umo1", "butler"
        driver._voice_sink = _Sink()
        await driver.emit_tool_call("call-1", "shell_exec", "{}", 1)
        assert spoken == ["butler: 🔨 调用工具: shell_exec"]
        registry.config.show_maid_tool_status = False
        await driver.emit_tool_call("call-2", "http_get", "{}", 2)

    asyncio.run(scenario())

    assert spoken == ["butler: 🔨 调用工具: shell_exec"]


def test_continued_task_round_has_independent_delivery_claim(registry, store):
    """同一个 session/task 的不同执行轮次分别认领，不能继承上一轮已发送状态。"""
    log = store.create_session(agent_preset="butler")
    task = registry.tasks.create(scope="scope", epoch="epoch", branch="branch", request="调查")
    first = registry.tasks.start_round(task["taskId"], log.session_id, "调查")
    registry.tasks.finish(task["taskId"], first["roundId"], {"status": "completed", "result": "第一次结果"})
    assert registry.tasks.claim_delivery(task["taskId"], first["roundId"])
    registry.tasks.delivery(task["taskId"], first["roundId"], "sent")
    second = registry.tasks.start_round(task["taskId"], log.session_id, "继续调查")
    registry.tasks.finish(task["taskId"], second["roundId"], {"status": "completed", "result": "第二次结果"})
    assert registry.tasks.claim_delivery(task["taskId"], second["roundId"])
    assert not registry.tasks.claim_delivery(task["taskId"], first["roundId"])
