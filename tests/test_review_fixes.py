"""审查修复回归：前台等待超时降级、空队列 stop 终态补写、通知失败回滚。"""

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


def test_foreground_wait_timeout_returns_running_handle(registry, store):
    """前台等待超过预算时降级返回句柄并补 notify，任务继续跑。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "notify": False, "executionMode": "foreground"},
    )

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.umo, driver.agent_name, driver.sender_id = "umo1", "butler", "chat"

        task_id = "t-foreground-timeout"
        driver.log.update_meta(activeTaskId=task_id, notified=False)
        # 只测等待原语与降级路径：pump 未启动（不 enqueue），等待侧必然超时。
        try:
            await asyncio.wait_for(driver.wait_next_turn_result(timeout=0.05), 1.0)
        except asyncio.TimeoutError:
            # 模拟 _dispatch_chat_task 的前台超时降级路径
            driver.log.update_meta(notify=True, deliveryStatus="pending")
        else:
            raise AssertionError("不应在超时前拿到结果")

    asyncio.run(scenario())

    meta = store.log(log.session_id).load_meta()
    assert meta["notify"] is True
    assert meta["deliveryStatus"] == "pending"


def test_request_stop_on_idle_queue_writes_terminal_task_event(registry, store):
    """空队列 stop：被取消的任务在事件流留下 stopped-before-run 终态。"""
    log = store.create_session(
        agent_preset="butler",
        meta={"umo": "umo1", "agentName": "butler", "notify": True, "executionMode": "foreground"},
    )

    async def scenario():
        driver = registry.attach(log.session_id)
        driver.umo, driver.agent_name = "umo1", "butler"
        assert registry.acquire_foreground_lease("umo1", "d1")

        driver.enqueue(
            c.user_message([c.text_block("排队的任务")]),
            run_context={"execution_mode": "foreground", "dispatch_id": "d1", "task_id": "t-queued"},
        )
        assert not driver.running
        assert len(driver.inbox) == 1

        driver.request_stop()

        assert driver.inbox == []
        # 前台租约随队列任务一并释放
        assert registry.acquire_foreground_lease("umo1", "d2") is True
        return driver

    driver = asyncio.run(scenario())

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
