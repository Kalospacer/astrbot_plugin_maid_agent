"""覆盖插件关闭时执行器退出，以及持久化任务的重载恢复。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pytest_asyncio
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.db import Database
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry, SessionDriver
from astrbot_plugin_maid_agent.harness.store import SessionStore
from astrbot_plugin_maid_agent.harness.tasks import (
    add_input,
    new_assignment,
    record_report,
    take_undelivered,
)


async def _execute_turn_probe(driver: SessionDriver, turn: int) -> dict:
    """替代外部模型执行，保留真实的 run_turn、收尾和 pump。"""
    probe = driver._test_execution
    probe.turns.append(turn)
    status = "completed"

    if probe.mode != "normal_resume" or turn > 2:
        probe.started.set()
        try:
            await probe.gate.wait()
        except asyncio.CancelledError:
            if probe.mode == "propagate":
                raise
            status = "interrupted"

    await driver._emit("turn/end", {"turn": turn, "reason": {"kind": status}})
    return {"status": status, "result": "", "error": ""}


@pytest_asyncio.fixture
async def driver_environment(tmp_path, monkeypatch):
    """提供真实临时存储与执行器，只替代外部模型和界面推送。"""
    db = Database(tmp_path / "maid.sqlite")
    store = SessionStore(tmp_path, db)
    context = SimpleNamespace()
    mux = SimpleNamespace(publish=Mock())
    host = SimpleNamespace(publish=Mock())
    registry = DriverRegistry(context, store, mux, host, MaidModeConfig())
    log = store.create_session(meta={"umo": "aiocqhttp:FriendMessage:114514", "sourceKind": "chat"})
    driver = registry.attach(log.session_id)
    driver._test_execution = SimpleNamespace(
        mode="consumed", started=asyncio.Event(), gate=asyncio.Event(), turns=[]
    )
    monkeypatch.setattr(SessionDriver, "_execute_turn", _execute_turn_probe)

    try:
        yield SimpleNamespace(
            db=db,
            store=store,
            context=context,
            mux=mux,
            host=host,
            registry=registry,
            driver=driver,
        )
    finally:
        if driver._task is not None and not driver._task.done():
            driver._task.cancel()
            await asyncio.gather(driver._task, return_exceptions=True)
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idle", "propagate", "consumed", "normal_resume"])
async def test_shutdown_finishes_after_interrupt(driver_environment, mode):
    """取消是否被轮次消费都不妨碍关闭，正常轮次仍可连续执行。"""
    env = driver_environment
    driver = env.driver
    probe = driver._test_execution
    probe.mode = mode

    if mode == "idle":
        driver._kick()
        await asyncio.sleep(0)
    else:
        count = 3 if mode == "normal_resume" else 1
        for _ in range(count):
            driver.enqueue(
                c.user_message([c.text_block("生命周期测试")]),
                run_context={"task_id": ""},
            )
        await asyncio.wait_for(probe.started.wait(), timeout=1)
        assert probe.turns == list(range(1, count + 1))

    pump = driver._task
    await asyncio.wait_for(env.registry.shutdown(), timeout=1)

    assert pump is not None and pump.done()
    assert driver.state == "idle"
    assert not driver.inbox
    driver._kick()
    assert driver._task is pump
    assert driver._task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unfinished", "pending", "waiting", "stopped"])
async def test_task_recovery_preserves_existing_semantics(driver_environment, state):
    """重新加载只重开未完成执行，保留排队和稳定点，不复活人工停止任务。"""
    env = driver_environment
    registry = env.registry
    umo = env.driver.umo
    chat = registry.chats.ensure(umo, "conversation-test")
    task = registry.tasks.create(
        scope=registry.chats.key(umo),
        epoch=chat["epoch"],
        umo=umo,
        conversation_id=chat["conversationId"],
        branch=c.new_id(),
        request="保留任务及上下文",
        strength="chat",
    )
    assignment = new_assignment(task, env.driver.session_id)
    add_input(task, "原始要求", human=True)

    if state == "pending":
        registry.queue_launch(task, assignment, human=False)
    else:
        assignment["state"] = "running"
        assignment["startedAt"] = c.now_ms()
        take_undelivered(task, assignment, read=True)
        if state == "waiting":
            report = record_report(task, state="waiting", summary="等待新输入")
            assignment["deliveredAt"] = report["at"] - 1
    registry.tasks.save(task)
    if state == "stopped":
        registry.stop_task(task)

    await asyncio.wait_for(registry.shutdown(), timeout=1)
    recovered = DriverRegistry(env.context, env.store, env.mux, env.host, MaidModeConfig())
    recovered.recover_open_tasks()
    recovered.recover_open_tasks()
    saved = recovered.tasks.get(task["taskId"])
    launches = recovered.effects.payloads("launch")

    assert saved["sessionId"] == env.driver.session_id
    assert saved["inputs"][0]["text"] == "原始要求"
    if state == "unfinished":
        assert len(saved["assignments"]) == 2
        assert saved["assignments"][0]["endedReason"] == "hostRestarted"
        assert saved["assignments"][1]["state"] == "pending"
        assert len(launches) == 1
        assert launches[0]["assignment_id"] == saved["assignments"][1]["id"]
    else:
        assert len(saved["assignments"]) == 1
        if state == "pending":
            assert saved["assignments"][0]["state"] == "pending"
            assert len(launches) == 1
        elif state == "waiting":
            assert saved["assignments"][0]["report"]["state"] == "waiting"
            assert launches == []
        else:
            assert saved["ended"]["outcome"] == "stopped"
            assert launches == []
