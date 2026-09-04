"""默认 subagent 自动创建：形状、幂等与落盘行为。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_maid_agent.config import MAID_AGENT_PERSONA
from astrbot_plugin_maid_agent.maid_dispatcher import (
    _default_subagent_entry,
    ensure_default_subagent,
)


class _Orchestrator:
    def __init__(self):
        self.reloaded_with = None

    async def reload_from_config(self, cfg):
        self.reloaded_with = cfg


class _Config(dict):
    def save_config(self):
        pass


class _Context:
    def __init__(self, agents):
        self._config = _Config({"subagent_orchestrator": {"agents": agents}})
        self.subagent_orchestrator = _Orchestrator()

    def get_config(self):
        return self._config


def _plugin_config():
    return SimpleNamespace(default_agent_name="butler")


def test_default_subagent_provisioning():
    entry = _default_subagent_entry("butler")
    assert entry["name"] == "butler"
    assert entry["enabled"] is True
    assert entry["system_prompt"] == MAID_AGENT_PERSONA
    assert entry["provider_id"] is None
    assert entry["tools"] is None

    # 已有任何条目（即使禁用）绝不覆盖
    ctx = _Context([{"name": "mine", "enabled": False}])
    assert asyncio.run(ensure_default_subagent(ctx, _plugin_config())) is False
    assert ctx.subagent_orchestrator.reloaded_with is None
    assert ctx._config["subagent_orchestrator"]["agents"] == [{"name": "mine", "enabled": False}]

    # 空配置时创建并 reload
    ctx = _Context([])
    assert asyncio.run(ensure_default_subagent(ctx, _plugin_config())) is True
    agents = ctx._config["subagent_orchestrator"]["agents"]
    assert [a["name"] for a in agents] == ["butler"]
    assert ctx.subagent_orchestrator.reloaded_with == {"agents": agents}
