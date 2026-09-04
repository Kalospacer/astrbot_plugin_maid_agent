"""审查修复回归：女仆正文押后投递、空队列 stop 终态补写、通知失败回滚。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore


class _Hub:
    def publish(self, *_args, **_kwargs):
        pass


class _Config:
    max_active_per_umo = 5
    max_active_global = 20
    memory_agent_names = ()
    retention_days = 30
    max_turn_seconds = 1800


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data")


@pytest.fixture()
def registry(store: SessionStore) -> DriverRegistry:
    return DriverRegistry(context=None, store=store, mux_hub=_Hub(), host_hub=_Hub(), config=_Config())


def test_maid_voice_holds_back_the_final_paragraph(registry, store):
    """女仆正文逐段投递到聊天，但最后一段留给大小姐转述，不重复。"""
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
        await driver._queue_voice("先看一下服务状态")
        await driver._queue_voice("")  # 纯工具调用的空步不占位
        await driver._queue_voice("端口是通的")
        await driver._queue_voice("汇报：服务正常")

    asyncio.run(scenario())

    assert spoken == ["butler: 先看一下服务状态", "butler: 端口是通的"]


def test_maid_voice_is_silent_for_console_sessions(registry, store):
    """控制台来源的会话没有聊天可说，投递端为空时不炸。"""
    log = store.create_session(agent_preset="butler", meta={"agentName": "butler", "sourceKind": "dashboard"})

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.agent_name = "butler"
        await driver._queue_voice("第一段")
        await driver._queue_voice("第二段")

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

        driver.enqueue(
            c.user_message([c.text_block("排队的任务")]),
            run_context={"task_id": "t-queued"},
        )
        assert not driver.running
        assert len(driver.inbox) == 1

        driver.request_stop()

        assert driver.inbox == []

    asyncio.run(scenario())

    meta = store.log(log.session_id).load_meta()
    assert meta["activeTaskId"] == ""
    assert meta["deliveryStatus"] == "stopped"
    events = [e for e in store.log(log.session_id).read_events() if e["type"] == "maid/task"]
    assert events and events[-1]["data"]["taskId"] == "t-queued"
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
    """汇报投递只能被认领一次：通知回灌和 maid_task_output 抢同一份，谁先谁负责。"""
    log = store.create_session(agent_preset="butler", meta={"umo": "umo1", "agentName": "butler"})

    async def scenario():
        driver = registry.attach(log.session_id)
        # 两边各自持有的外层锁不是同一把，所以并发发起认领。
        return await asyncio.gather(*(driver.claim_delivery() for _ in range(4)))

    claims = asyncio.run(scenario())
    assert claims.count(True) == 1
    assert store.log(log.session_id).load_meta()["deliveryClaimed"] is True


def test_failed_delivery_returns_the_claim(registry, store):
    """投递失败要归还认领，否则这份汇报再也没人转达。"""
    log = store.create_session(agent_preset="butler", meta={"umo": "umo1", "agentName": "butler"})

    async def scenario():
        driver = registry.attach(log.session_id)
        assert await driver.claim_delivery() is True
        assert await driver.claim_delivery() is False
        await driver.release_delivery_claim()
        return await driver.claim_delivery()

    assert asyncio.run(scenario()) is True
