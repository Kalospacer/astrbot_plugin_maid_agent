"""孤儿 turn 自愈与看门狗取消测试（桩 hub，无 astrbot 运行时依赖）。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore


class _Hub:
    def publish(self, *args, **kwargs):
        pass


def sync(coro_fn):
    import functools

    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))

    return wrapper


@pytest.fixture()
def registry(tmp_path: Path) -> DriverRegistry:
    store = SessionStore(tmp_path / "runtime")
    return DriverRegistry(
        context=None,  # 测试不执行真实 turn
        store=store,
        mux_hub=_Hub(),
        host_hub=_Hub(),
        config=None,
    )


def _create_session_with_orphan_turn(registry: DriverRegistry) -> str:
    log = registry.store.create_session(meta={"umo": ""})
    sid = log.session_id
    log.append("turn/start", {"turn": 1})
    log.append(
        "user/message",
        c.user_message([c.text_block("被杀进程打断的请求")]),
        source_event_seqs=[],
    )
    return sid


class TestOrphanTurnHeal:
    def test_orphan_turn_closed_on_attach(self, registry: DriverRegistry):
        sid = _create_session_with_orphan_turn(registry)
        driver = registry.driver(sid)
        assert driver is not None
        events = driver.log.read_events()
        last = events[-1]
        assert last["type"] == "turn/end"
        assert last["data"]["reason"]["kind"] == "interrupted"

    def test_heal_is_idempotent(self, registry: DriverRegistry):
        sid = _create_session_with_orphan_turn(registry)
        registry.driver(sid)
        count_after_first = len(registry.store.log(sid).read_events())
        # 重建 registry（模拟再次重启/重新 attach），不应重复补写
        fresh = DriverRegistry(
            context=None, store=registry.store, mux_hub=_Hub(), host_hub=_Hub(), config=None
        )
        fresh.driver(sid)
        assert len(registry.store.log(sid).read_events()) == count_after_first
        assert [e["type"] for e in registry.store.log(sid).read_events()].count("turn/end") == 1

    def test_closed_turn_untouched(self, registry: DriverRegistry):
        log = registry.store.create_session(meta={"umo": ""})
        sid = log.session_id
        log.append("turn/start", {"turn": 1})
        log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})
        registry.driver(sid)
        types = [e["type"] for e in registry.store.log(sid).read_events()]
        assert types.count("turn/end") == 1  # 未新增

    def test_empty_log_untouched(self, registry: DriverRegistry):
        log = registry.store.create_session(meta={"umo": ""})
        sid = log.session_id
        driver = registry.driver(sid)
        assert driver is not None
        assert all(e["type"] != "turn/end" for e in driver.log.read_events())


class TestWatchdogCancel:
    @sync
    async def test_cancelled_turn_settles_and_pump_survives(self, registry: DriverRegistry):
        log = registry.store.create_session(meta={"umo": ""})
        sid = log.session_id
        driver = registry.driver(sid)
        assert driver is not None

        async def hang(_message):
            driver.state = "running"
            driver.turn_started_at = time.monotonic()
            try:
                await asyncio.sleep(3600)
            finally:
                driver.turn_started_at = None
                driver.state = "idle"

        driver.run_turn = hang  # type: ignore[assignment]
        driver.enqueue(c.user_message([c.text_block("hi")]))

        for _ in range(50):
            if driver.state == "running":
                break
            await asyncio.sleep(0.02)
        assert driver.state == "running"

        driver.watchdog_cancel()
        for _ in range(100):
            if driver.state == "idle":
                break
            await asyncio.sleep(0.02)

        assert driver.state == "idle"
        assert driver.turn_started_at is None
        events = driver.log.read_events()
        assert events[-1]["type"] == "turn/end"
        assert events[-1]["data"]["reason"]["kind"] == "interrupted"
        assert driver.last_turn.get("status") == "interrupted"
        # pump 仍存活（可继续处理后续消息），没有被取消波及
        assert driver._task is not None and not driver._task.done()

        driver.interrupt()
        driver._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await driver._task

    def test_watchdog_cancel_noop_without_task(self, registry: DriverRegistry):
        log = registry.store.create_session(meta={"umo": ""})
        sid = log.session_id
        driver = registry.driver(sid)
        assert driver is not None
        driver.watchdog_cancel()  # 无任务时不抛错
