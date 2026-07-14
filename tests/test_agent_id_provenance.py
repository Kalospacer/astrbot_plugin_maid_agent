from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import astrbot_plugin_maid_agent.session_store as session_store_module
from astrbot_plugin_maid_agent.config import MaidModeConfig
from astrbot_plugin_maid_agent.maid_dispatcher import (
    _checkpoint_runner_session,
    _sanitize_subagent_toolset,
)
from astrbot_plugin_maid_agent.main import MaidAgent
from astrbot_plugin_maid_agent.session_store import (
    MaidAgentSession,
    MaidSessionStore,
)

from astrbot.core.agent.message import Message
from astrbot.core.agent.tool import FunctionTool, ToolSet


class _KvPlugin:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def get_kv_data(self, key: str, default):
        return self.data.get(key, default)

    async def put_kv_data(self, key: str, value) -> None:
        self.data[key] = value


def test_session_provenance_round_trip_and_legacy_compatibility() -> None:
    session = MaidAgentSession.create(
        "platform:FriendMessage:user",
        "butler",
        agent_id="agent-1",
        parent_message_id="message-1",
        owner_sender_id="owner-1",
    )

    restored = MaidAgentSession.from_dict(session.to_dict())
    assert restored.agent_id == "agent-1"
    assert restored.parent_message_id == "message-1"
    assert restored.owner_sender_id == "owner-1"

    legacy = session.to_dict()
    legacy.pop("agent_id")
    legacy.pop("parent_message_id")
    legacy.pop("owner_sender_id")
    restored_legacy = MaidAgentSession.from_dict(legacy)
    assert restored_legacy.agent_id == ""
    assert restored_legacy.parent_message_id == ""
    assert restored_legacy.owner_sender_id == ""


def test_reused_session_keeps_root_agent_and_refreshes_parent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        session_store_module.StarTools,
        "get_data_dir",
        lambda _plugin_name: tmp_path,
    )

    async def scenario() -> None:
        store = MaidSessionStore(_KvPlugin(), MaidModeConfig())
        first, reused = await store.get_or_create_active_session(
            "platform:FriendMessage:user",
            "butler",
            agent_id="agent-root",
            parent_message_id="message-1",
            owner_sender_id="owner-1",
        )
        assert reused is False

        second, reused = await store.get_or_create_active_session(
            "platform:FriendMessage:user",
            "butler",
            agent_id="agent-next",
            parent_message_id="message-2",
            owner_sender_id="owner-2",
        )
        assert reused is True
        assert second.session_id == first.session_id
        assert second.agent_id == "agent-root"
        assert second.parent_message_id == "message-2"
        assert second.owner_sender_id == "owner-2"

    asyncio.run(scenario())


class _ConversationManager:
    def __init__(self, history: list[dict]) -> None:
        self.history = history

    async def get_conversation(self, _umo: str, _cid: str):
        return SimpleNamespace(history=json.dumps(self.history, ensure_ascii=False))

    async def update_conversation(self, _umo: str, _cid: str, *, history) -> None:
        self.history = history


class _Event:
    unified_msg_origin = "platform:FriendMessage:user"

    @staticmethod
    def get_result():
        return None


def test_agent_id_anchor_survives_invalid_request_history() -> None:
    async def scenario() -> None:
        manager = _ConversationManager([{"role": "assistant", "content": "先去看看。"}])
        plugin = object.__new__(MaidAgent)
        plugin.context = SimpleNamespace(conversation_manager=manager)
        plugin._conversation_history_locks = {}
        request = SimpleNamespace(
            conversation=SimpleNamespace(cid="conversation-1", history="not-json")
        )
        event = _Event()

        await plugin._persist_assistant_reply(
            event,
            request,
            "已经处理好了。",
            agent_id="agent-1",
        )
        await plugin._persist_call_maid_tool_history(
            event,
            request,
            [
                {
                    "action": "dispatch",
                    "request_text": "处理任务",
                    "agent_name": "butler",
                    "tool_result": "完成",
                }
            ],
            agent_id="agent-1",
        )

        anchor_index = next(
            index
            for index, message in enumerate(manager.history)
            if message.get("_maid_agent_id") == "agent-1"
        )
        assert manager.history[anchor_index]["content"] == "已经处理好了。"
        assert manager.history[anchor_index - 1]["role"] == "tool"
        assert manager.history[anchor_index - 2]["role"] == "assistant"
        assert manager.history[anchor_index - 2]["tool_calls"][0]["function"]["name"] == "call_maid"

    asyncio.run(scenario())


def test_agent_id_history_metadata_is_not_sent_to_provider() -> None:
    message = Message.model_validate(
        {
            "role": "assistant",
            "content": "完成",
            "_maid_agent_id": "agent-1",
        }
    )
    assert "_maid_agent_id" not in message.model_dump()


def test_runner_checkpoint_persists_each_complete_step() -> None:
    class _Store:
        def __init__(self) -> None:
            self.calls = 0

        async def save_session_if_active(self, _session, *, require_active_session_id: bool):
            self.calls += 1
            assert require_active_session_id is True
            return True

    async def scenario() -> None:
        store = _Store()
        session = MaidAgentSession.create("umo", "butler")
        runner = SimpleNamespace(
            run_context=SimpleNamespace(
                messages=[
                    Message(role="user", content="做任务"),
                    Message(role="assistant", content="阶段结果"),
                ]
            )
        )

        persisted = await _checkpoint_runner_session(
            session=session,
            session_store=store,
            runner=runner,
            maid_request="做任务",
            explicit_session_id=None,
        )
        assert persisted is True
        assert store.calls == 1
        assert session.last_maid_request == "做任务"
        assert session.last_agent_result == "阶段结果"
        assert session.messages[-1]["role"] == "assistant"

    asyncio.run(scenario())


def test_subagent_toolset_cannot_recursively_call_maid() -> None:
    toolset = ToolSet(
        [
            FunctionTool(
                name="call_maid",
                description="control plane",
                parameters={"type": "object", "properties": {}},
            ),
            FunctionTool(
                name="safe_tool",
                description="worker tool",
                parameters={"type": "object", "properties": {}},
            ),
        ]
    )

    sanitized = _sanitize_subagent_toolset(toolset)
    assert sanitized is not None
    assert sanitized.get_tool("call_maid") is None
    assert sanitized.get_tool("safe_tool") is not None
