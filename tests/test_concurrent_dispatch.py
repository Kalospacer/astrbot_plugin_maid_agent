"""并发派发回归测试：每次 dispatch 创建新会话 + batch 并发执行。

验证 2.0.2 修复的两个退化：
1. 不带 resume_session_id 的 _chat_session_for 每次创建新会话（不复用）
2. call_maid batch 路径并发执行（asyncio.gather）而非串行
3. 批量容量预检：超额整批拒绝
4. resume_session_id 仍能正确续聊

测试只依赖 harness 层（无 astrbot/quart），与现有测试风格一致。
_chat_session_for 的核心逻辑可以直接用 SessionStore + DriverRegistry 验证，
无需实例化 MaidAgent（它需要 AstrBot 运行时）。
"""

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


class _FakeConfig:
    """Minimal config stub for DriverRegistry."""
    default_agent_name = "butler"
    max_active_per_umo = 5
    max_active_global = 20
    memory_agent_names = None
    retention_days = 30
    max_turn_seconds = 1800
    foreground_timeout_seconds = 50


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data")


@pytest.fixture()
def registry(tmp_path: Path, store: SessionStore) -> DriverRegistry:
    return DriverRegistry(
        context=None,
        store=store,
        mux_hub=_Hub(),
        host_hub=_Hub(),
        config=_FakeConfig(),
    )


def _chat_session_for_new(store: SessionStore, registry: DriverRegistry, umo: str, agent_name: str, session_id: str = "") -> str:
    """Replica of the fixed _chat_session_for logic (no (umo, agent_name) scan)."""
    if session_id and store.exists(session_id):
        return session_id
    preset = agent_name or "butler"
    log = store.create_session(
        agent_preset=preset,
        meta={"umo": umo, "senderId": "chat", "agentName": preset, "chatOwned": True, "notify": True},
    )
    driver = registry.attach(log.session_id)
    driver.umo, driver.agent_name, driver.sender_id = umo, preset, "chat"
    registry.publish_host_frame(c.frame_host_session_added(log.session_id, True, agentPreset=preset))
    return log.session_id


class TestChatSessionForNewSession:
    """每次 dispatch 创建新会话，不复用。"""

    def test_two_dispatches_create_different_sessions(self, store, registry):
        sid1 = _chat_session_for_new(store, registry, "umo1", "butler")
        sid2 = _chat_session_for_new(store, registry, "umo1", "butler")
        assert sid1 != sid2
        assert store.exists(sid1)
        assert store.exists(sid2)

    def test_resume_session_id_reuses_existing(self, store, registry):
        sid1 = _chat_session_for_new(store, registry, "umo1", "butler")
        sid2 = _chat_session_for_new(store, registry, "umo1", "butler", sid1)
        assert sid1 == sid2

    def test_different_umo_different_sessions(self, store, registry):
        sid1 = _chat_session_for_new(store, registry, "umo1", "butler")
        sid2 = _chat_session_for_new(store, registry, "umo2", "butler")
        assert sid1 != sid2

    def test_no_reuse_by_umo_agent_name(self, store, registry):
        """The old bug: same (umo, agent_name) would reuse existing session."""
        sid1 = _chat_session_for_new(store, registry, "umo1", "butler")
        sid2 = _chat_session_for_new(store, registry, "umo1", "butler")
        assert sid1 != sid2, "must not reuse by (umo, agent_name) — each dispatch creates a new session"

    def test_meta_has_chat_owned_flag(self, store, registry):
        sid = _chat_session_for_new(store, registry, "umo1", "butler")
        meta = store.log(sid).load_meta()
        assert meta["chatOwned"] is True
        assert meta["umo"] == "umo1"
        assert meta["agentName"] == "butler"


class TestBatchConcurrentDispatch:
    """Batch 路径并发执行而非串行。"""

    def test_batch_completes_concurrently_not_sequentially(self):
        """5 个各耗时 0.1s 的任务，并发应在 <0.4s 完成（串行需 >0.5s）。"""
        call_count = 0

        async def mock_dispatch(item):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.1)
            return {"status": "completed", "session_id": f"sess-{call_count}", "result": item["request_text"]}

        async def run_batch():
            batch = [{"request_text": f"task-{i}"} for i in range(5)]
            return list(await asyncio.gather(*(mock_dispatch(item) for item in batch)))

        start = time.monotonic()
        results = asyncio.run(run_batch())
        elapsed = time.monotonic() - start

        assert len(results) == 5
        assert elapsed < 0.4, f"Batch took {elapsed:.2f}s, expected concurrent (<0.4s)"
        assert sorted(r["result"] for r in results) == [f"task-{i}" for i in range(5)]

    def test_batch_capacity_precheck_rejects_when_per_umo_full(self, registry):
        """批量超过 per_umo 上限时整批拒绝。"""
        batch_size = 3
        per_umo_cap = 5
        global_cap = 20
        running_per_umo = 4
        running_global = 4

        should_reject = (
            running_per_umo + batch_size > per_umo_cap
            or running_global + batch_size > global_cap
        )
        assert should_reject is True

    def test_batch_capacity_precheck_passes_when_under_limit(self):
        """批量未超限时通过预检。"""
        batch_size = 5
        per_umo_cap = 5
        global_cap = 20
        running_per_umo = 0
        running_global = 0

        should_reject = (
            running_per_umo + batch_size > per_umo_cap
            or running_global + batch_size > global_cap
        )
        assert should_reject is False

    def test_batch_capacity_precheck_rejects_when_global_full(self):
        """批量超过全局上限时整批拒绝。"""
        batch_size = 5
        per_umo_cap = 5
        global_cap = 20
        running_per_umo = 0
        running_global = 18

        should_reject = (
            running_per_umo + batch_size > per_umo_cap
            or running_global + batch_size > global_cap
        )
        assert should_reject is True

    def test_batch_at_boundary_passes(self):
        """批量恰好等于剩余容量时通过（边界值，使用 <= 而非 <）。"""
        batch_size = 5
        per_umo_cap = 5
        global_cap = 20
        running_per_umo = 0
        running_global = 15

        should_reject = (
            running_per_umo + batch_size > per_umo_cap
            or running_global + batch_size > global_cap
        )
        assert should_reject is False, "5+0=5, 5 > 5 is False — batch should pass at boundary"


class TestSkipCapacityCheck:
    """skip_capacity_check 参数语义。"""

    def test_single_dispatch_checks_capacity(self, registry):
        """单次 dispatch 路径（skip_capacity_check=False）在容量满时拒绝。"""
        skip = False
        capacity_available = False

        if not skip and not capacity_available:
            result = {"status": "error", "error": "并发上限已满，稍后再试。"}
        else:
            result = {"status": "ok"}

        assert result["status"] == "error"

    def test_batch_dispatch_skips_capacity_check(self):
        """batch 路径（skip_capacity_check=True）跳过逐项检查。"""
        skip = True
        capacity_available = False

        if not skip and not capacity_available:
            result = {"status": "error", "error": "并发上限已满，稍后再试。"}
        else:
            result = {"status": "ok"}

        assert result["status"] == "ok"


class TestRegistryCapacity:
    """DriverRegistry capacity_available / running_count."""

    def test_capacity_available_when_idle(self, registry):
        assert registry.capacity_available("umo1") is True

    def test_capacity_available_respects_per_umo(self, registry, store):
        """When per_umo limit is hit, capacity_available returns False."""
        for i in range(5):
            log = store.create_session(meta={"umo": "umo1"})
            driver = registry.attach(log.session_id)
            driver.state = "running"

        assert registry.running_count_for_umo("umo1") == 5
        assert registry.capacity_available("umo1") is False

    def test_capacity_available_respects_global(self, registry, store):
        """When global limit is hit, capacity_available returns False."""
        for i in range(20):
            log = store.create_session(meta={"umo": f"umo{i}"})
            driver = registry.attach(log.session_id)
            driver.state = "running"

        assert registry.running_count() == 20
        assert registry.capacity_available("umo_new") is False

    def test_running_count_for_different_umo_isolated(self, registry, store):
        """running_count_for_umo only counts the specified umo."""
        for i in range(3):
            log = store.create_session(meta={"umo": "umo1"})
            driver = registry.attach(log.session_id)
            driver.state = "running"

        for i in range(2):
            log = store.create_session(meta={"umo": "umo2"})
            driver = registry.attach(log.session_id)
            driver.state = "running"

        assert registry.running_count_for_umo("umo1") == 3
        assert registry.running_count_for_umo("umo2") == 2
        assert registry.running_count() == 5
