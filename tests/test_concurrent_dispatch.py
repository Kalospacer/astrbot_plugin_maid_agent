"""Foreground lease and capacity tests for the isolated driver registry."""

from __future__ import annotations

from pathlib import Path

import pytest

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
def registry(tmp_path: Path) -> DriverRegistry:
    return DriverRegistry(None, SessionStore(tmp_path / "data"), _Hub(), _Hub(), _Config())


def test_one_foreground_lease_per_umo(registry):
    assert registry.acquire_foreground_lease("qq:GroupMessage:1", "dispatch-a") is True
    assert registry.acquire_foreground_lease("qq:GroupMessage:1", "dispatch-b") is False
    assert registry.acquire_foreground_lease("qq:GroupMessage:2", "dispatch-c") is True


def test_release_requires_the_current_dispatch_identity(registry):
    registry.acquire_foreground_lease("umo", "dispatch-a")
    registry.release_foreground_lease("umo", "wrong")
    assert registry.acquire_foreground_lease("umo", "dispatch-b") is False
    registry.release_foreground_lease("umo", "dispatch-a")
    assert registry.acquire_foreground_lease("umo", "dispatch-b") is True


def test_batch_allocation_is_deterministic_by_input_order(registry):
    modes = []
    for dispatch_id in ("first", "second", "third"):
        foreground = registry.acquire_foreground_lease("umo", dispatch_id)
        modes.append("foreground" if foreground else "background")
    assert modes == ["foreground", "background", "background"]


def test_running_capacity_remains_independent_of_foreground_lease(registry):
    assert registry.capacity_available("umo") is True
    registry.acquire_foreground_lease("umo", "dispatch-a")
    assert registry.capacity_available("umo") is True
