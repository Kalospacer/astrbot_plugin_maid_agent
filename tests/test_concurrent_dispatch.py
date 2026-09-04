"""Concurrency tests for the isolated driver registry.

前台租约已随执行模式一起删除，同一 UMO 的并发现在只由容量上限约束。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore


class _Hub:
    def publish(self, *_args, **_kwargs):
        pass


class _Config:
    max_active_per_umo = 2
    max_active_global = 3
    memory_agent_names = ()
    retention_days = 30
    max_turn_seconds = 1800


@pytest.fixture()
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "data")


@pytest.fixture()
def registry(store: SessionStore) -> DriverRegistry:
    return DriverRegistry(None, store, _Hub(), _Hub(), _Config())


def _running_driver(registry: DriverRegistry, store: SessionStore, umo: str):
    log = store.create_session(agent_preset="butler", meta={"umo": umo, "agentName": "butler"})
    driver = registry.attach(log.session_id)
    driver.umo = umo
    driver.state = "running"
    return driver


def test_capacity_is_bounded_per_umo(registry, store):
    for _ in range(_Config.max_active_per_umo):
        _running_driver(registry, store, "qq:GroupMessage:1")
    assert registry.capacity_available("qq:GroupMessage:1") is False
    assert registry.capacity_available("qq:GroupMessage:2") is True


def test_capacity_is_bounded_globally(registry, store):
    _running_driver(registry, store, "umo-a")
    _running_driver(registry, store, "umo-b")
    _running_driver(registry, store, "umo-c")
    assert registry.running_count() == _Config.max_active_global
    assert registry.capacity_available("umo-d") is False


def test_idle_drivers_do_not_consume_capacity(registry, store):
    driver = _running_driver(registry, store, "umo")
    assert registry.running_count_for_umo("umo") == 1
    driver.state = "idle"
    assert registry.running_count_for_umo("umo") == 0
    assert registry.capacity_available("umo") is True
