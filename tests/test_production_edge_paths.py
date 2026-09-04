"""关键生产路径：历史工具结果恢复与共享 provider 配置隔离。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from astrbot_plugin_maid_agent import maid_dispatcher
from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.api import ApiProxy
from astrbot_plugin_maid_agent.harness.drivers import DriverRegistry
from astrbot_plugin_maid_agent.harness.store import SessionStore

from astrbot.core.agent.message import Message


class _Hub:
    def publish(self, *_args, **_kwargs):
        pass


class _Config:
    max_active_per_umo = 5
    max_active_global = 20


class _ConfigHolder:
    def get_config(self):
        return {}

    def settings_schema(self):
        return {}

    def save_config(self, _patch):
        return {}

    def version(self):
        return "test"


@pytest.fixture()
def store(tmp_path):
    return SessionStore(tmp_path / "data")


@pytest.fixture()
def registry(store):
    return DriverRegistry(context=None, store=store, mux_hub=_Hub(), host_hub=_Hub(), config=_Config())


def _tool_result_text(event):
    return event["data"]["message"]["content"][0]["content"][0]["text"]


def test_rebuild_context_preserves_completed_tool_output(store, registry):
    """续聊必须将工具返回值送回 runner，否则后续回答会失去已查询的信息。"""
    log = store.create_session(meta={"umo": "qq:GroupMessage:1"})
    log.append("turn/start", {"turn": 1})
    log.append("user/message", c.user_message([c.text_block("查天气")]), source_event_seqs=[])
    log.append(
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": c.assistant_message(
                [c.tool_call_block("call-1", "web_search", '{"query":"weather"}')], "p", "m"
            ),
        },
        source_event_seqs=[],
    )
    log.append(
        "tool/result",
        {
            "turn": 1,
            "step": 1,
            "message": c.tool_result_message("call-1", [c.text_block("晴，28°C")], False),
        },
        source_event_seqs=[],
    )
    log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})

    contexts = registry.attach(log.session_id)._rebuild_contexts(before_seq=log.last_seq + 1)

    assert [(message.role, message.content) for message in contexts] == [
        ("user", "查天气"),
        ("assistant", None),
        ("tool", "晴，28°C"),
    ]
    assert contexts[1].tool_calls[0].id == "call-1"
    assert contexts[2].tool_call_id == "call-1"


def test_history_replays_tool_result_view_with_original_output(store):
    """重新打开控制台历史时，工具卡片也必须展示持久化的实际输出。"""
    log = store.create_session()
    log.append("turn/start", {"turn": 1})
    log.append("tool/call", {"turn": 1, "step": 1, "callId": "call-1", "name": "shell", "arguments": '{"command":"date"}'})
    log.append(
        "tool/result",
        {
            "turn": 1,
            "step": 1,
            "message": c.tool_result_message("call-1", [c.text_block("2026-09-03")], False),
        },
        source_event_seqs=[],
    )
    log.append("turn/end", {"turn": 1, "reason": c.reason_completed()})
    api = ApiProxy(store=store, registry=SimpleNamespace(), config_holder=_ConfigHolder())

    page = asyncio.run(api.session_history({"sessionId": log.session_id}))
    result_entry = next(entry for entry in page["events"] if entry["event"]["type"] == "tool/result")

    assert _tool_result_text(result_entry["event"]) == "2026-09-03"
    assert result_entry["view"]["view"]["output"] == "2026-09-03"


class _Provider:
    def __init__(self):
        self.provider_config = {"max_context_tokens": 64}


class _ConcurrentRunner:
    active = 0
    max_active = 0
    observed_contexts = []

    async def reset(self, **kwargs):
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        type(self).observed_contexts.append(kwargs["request"].contexts)
        await asyncio.sleep(0)
        type(self).active -= 1


def test_build_runner_serializes_provider_config_mutation(monkeypatch):
    """共享 provider 的临时 context 上限不得泄漏给并发会话。"""
    _ConcurrentRunner.active = 0
    _ConcurrentRunner.max_active = 0
    _ConcurrentRunner.observed_contexts = []
    monkeypatch.setattr(maid_dispatcher, "ToolLoopAgentRunner", _ConcurrentRunner)
    monkeypatch.setattr(maid_dispatcher, "AstrAgentContext", lambda **kwargs: kwargs)
    monkeypatch.setattr(maid_dispatcher, "AgentContextWrapper", lambda **kwargs: kwargs)
    monkeypatch.setattr(maid_dispatcher, "FunctionToolExecutor", lambda: object())
    provider = _Provider()

    async def build():
        return await maid_dispatcher._build_runner(
            context=object(),
            event=object(),
            provider=provider,
            prompt="current",
            image_urls=[],
            system_prompt="system",
            tools=None,
            contexts=[Message(role="user", content="previous")],
            stream=False,
            tool_call_timeout=60,
            llm_compress_instruction="",
            llm_compress_keep_recent=4,
            llm_compress_provider=None,
            truncate_turns=1,
            enforce_max_turns=-1,
            tool_schema_mode="full",
            max_context_tokens=2048,
            session_id="session-1",
            agent_hooks=object(),
        )

    async def build_both():
        await asyncio.gather(build(), build())

    asyncio.run(build_both())

    assert _ConcurrentRunner.max_active == 1
    assert provider.provider_config["max_context_tokens"] == 64
    assert all(contexts[0]["content"] == "previous" for contexts in _ConcurrentRunner.observed_contexts)


def test_build_runner_restores_missing_provider_limit_after_reset_failure(monkeypatch):
    class _FailingRunner:
        async def reset(self, **_kwargs):
            raise RuntimeError("reset failed")

    monkeypatch.setattr(maid_dispatcher, "ToolLoopAgentRunner", _FailingRunner)
    monkeypatch.setattr(maid_dispatcher, "AstrAgentContext", lambda **kwargs: kwargs)
    monkeypatch.setattr(maid_dispatcher, "AgentContextWrapper", lambda **kwargs: kwargs)
    monkeypatch.setattr(maid_dispatcher, "FunctionToolExecutor", lambda: object())
    provider = _Provider()
    provider.provider_config.clear()

    async def build():
        await maid_dispatcher._build_runner(
            context=object(), event=object(), provider=provider, prompt="", image_urls=[], system_prompt="",
            tools=None, contexts=None, stream=False, tool_call_timeout=60, llm_compress_instruction="",
            llm_compress_keep_recent=4, llm_compress_provider=None, truncate_turns=1,
            enforce_max_turns=-1, tool_schema_mode="full", max_context_tokens=2048,
            session_id="session-1", agent_hooks=object(),
        )

    with pytest.raises(RuntimeError, match="reset failed"):
        asyncio.run(build())
    assert "max_context_tokens" not in provider.provider_config
