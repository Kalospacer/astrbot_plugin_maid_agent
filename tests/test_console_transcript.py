"""Agent transcript payload builder and route regressions (1.4.0)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrbot_plugin_maid_agent.main import MaidAgent
from astrbot_plugin_maid_agent.runtime_store import (
    CTRL_RUN_END,
    CTRL_RUN_START,
    CTRL_STEER,
    CTRL_TOOL_END,
    CTRL_TOOL_START,
)

AGENT_ID = "a" * 32
TASK_1 = "1" * 32
TASK_2 = "2" * 32


def _control(kind: str, task_id: str = "", **payload) -> dict:
    return {"_control": True, "kind": kind, "task_id": task_id, **payload}


def _message(role: str, content, task_id: str = "") -> dict:
    record = {"role": role, "content": content}
    if task_id:
        record["task_id"] = task_id
    return record


def _build(records: list[dict]) -> dict:
    plugin = object.__new__(MaidAgent)
    return plugin._build_agent_transcript_payload(AGENT_ID, records)


def test_transcript_empty_records_returns_no_runs() -> None:
    payload = _build([])
    assert payload == {"agent_id": AGENT_ID, "runs": []}


def test_transcript_splits_two_runs_in_order() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "【大小姐请求】\nfirst task", TASK_1),
        _message("assistant", "first answer", TASK_1),
        _control(CTRL_RUN_END, TASK_1),
        _control(CTRL_RUN_START, TASK_2),
        _message("user", "【大小姐请求】\nsecond task", TASK_2),
        _control(CTRL_RUN_END, TASK_2),
    ]

    payload = _build(records)

    assert [run["task_id"] for run in payload["runs"]] == [TASK_1, TASK_2]
    first, second = payload["runs"]
    # 无结构化字段的历史记录经兜底解析：【大小姐请求】块拆入 mistress_text，
    # 无【对方原话】块时 user_text 为空。
    assert first["user_text"] == ""
    assert first["mistress_text"] == "first task"
    assert second["user_text"] == ""
    assert second["mistress_text"] == "second task"
    assert first["steers"] == []
    # 第一段有一条 assistant 输出，第二段没有。
    first_kinds = [entry["kind"] for entry in first["tool_chain"]["entries"]]
    second_kinds = [entry["kind"] for entry in second["tool_chain"]["entries"]]
    assert first_kinds == ["assistant"]
    assert second_kinds == []


def test_transcript_structured_fields_split_user_and_mistress() -> None:
    # 新数据：结构化字段为真源，绝不把 dispatch 模板正文带进大小姐气泡。
    records = [
        _control(CTRL_RUN_START, TASK_1),
        {
            "role": "user",
            "content": "【对方原话】\nhello\n\n【大小姐请求】\nsummarize it\n\n你是MuiceMaid……",
            "task_id": TASK_1,
            "user_input": "hello",
            "mistress_request": "summarize it",
        },
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    run = payload["runs"][0]
    assert run["user_text"] == "hello"
    assert run["mistress_text"] == "summarize it"


def test_transcript_marker_fallback_without_structured_fields() -> None:
    # 历史 transcript：无结构化字段，从拼接 content 兜底解析出两块纯文本。
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "【对方原话】\nhello\n\n【大小姐请求】\nsummarize it\n\n", TASK_1),
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    run = payload["runs"][0]
    assert run["user_text"] == "hello"
    assert run["mistress_text"] == "summarize it"


def test_transcript_mistress_only_leaves_user_text_empty() -> None:
    # 仅有大小姐请求（无对方原话）：user_text 空 → 前端跳过 user 气泡，
    # 只出大小姐气泡，避免同一句在两处重复显示。
    records = [
        _control(CTRL_RUN_START, TASK_1),
        {
            "role": "user",
            "content": "【大小姐请求】\ndo it",
            "task_id": TASK_1,
            "user_input": "",
            "mistress_request": "do it",
        },
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    run = payload["runs"][0]
    assert run["user_text"] == ""
    assert run["mistress_text"] == "do it"


def test_transcript_steer_attaches_to_open_segment() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _control(CTRL_STEER, TASK_1, message="补充一下要求"),
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    assert payload["runs"][0]["steers"] == ["补充一下要求"]


def test_transcript_steer_between_runs_attaches_to_last_segment() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _control(CTRL_RUN_END, TASK_1),
        # run_end 之后、下一个 run_start 之前到达的 steer。
        _control(CTRL_STEER, TASK_1, message="迟到的补充"),
    ]

    payload = _build(records)

    assert payload["runs"][0]["steers"] == ["迟到的补充"]


def test_transcript_orphan_messages_merge_into_first_segment() -> None:
    records = [
        # begin_dialogs：首个 run_start 之前的孤儿消息。
        _message("assistant", "preset dialog"),
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    assert len(payload["runs"]) == 1
    run = payload["runs"][0]
    assert run["task_id"] == TASK_1
    # 孤儿 assistant 消息并入第一段参与 tool_chain 构建。
    kinds = [entry["kind"] for entry in run["tool_chain"]["entries"]]
    assert kinds == ["assistant"]
    # 孤儿消息本身保留，orphan_count 只统计游离控制记录。
    assert run["orphan_count"] == 0


def test_transcript_messages_without_task_id_fall_into_current_segment() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _message("assistant", "no task_id message"),
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    run = payload["runs"][0]
    kinds = [entry["kind"] for entry in run["tool_chain"]["entries"]]
    assert kinds == ["assistant"]


def test_transcript_tool_chain_from_control_records() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _control(
            CTRL_TOOL_START,
            TASK_1,
            tool_call_id="call-1",
            tool_name="search",
            arguments={"q": "kimi k3"},
        ),
        _control(
            CTRL_TOOL_END,
            TASK_1,
            tool_call_id="call-1",
            tool_name="search",
            result="found it",
        ),
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    kinds = [entry["kind"] for entry in payload["runs"][0]["tool_chain"]["entries"]]
    assert kinds == ["tool_call", "tool_result"]
    call = payload["runs"][0]["tool_chain"]["entries"][0]
    assert call["tool_name"] == "search"
    assert call["tool_call_id"] == "call-1"


def test_transcript_tool_chain_from_assistant_tool_calls() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-9",
                    "function": {"name": "read_file", "arguments": '{"path": "/a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-9", "content": "file body"},
        _control(CTRL_RUN_END, TASK_1),
    ]

    payload = _build(records)

    kinds = [entry["kind"] for entry in payload["runs"][0]["tool_chain"]["entries"]]
    assert kinds == ["tool_call", "tool_result"]
    assert payload["runs"][0]["tool_chain"]["entries"][0]["tool_name"] == "read_file"


def _async_wrap(value):
    async def _f(*_args, **_kwargs):
        return value

    return _f


def _make_route_plugin(records: list[dict]) -> MaidAgent:
    plugin = object.__new__(MaidAgent)
    meta = SimpleNamespace(agent_id=AGENT_ID, to_dict=lambda: {"agent_id": AGENT_ID})
    plugin.runtime_store = SimpleNamespace(
        load_agent=_async_wrap(meta),
        load_transcript=_async_wrap(records),
        list_runs=_async_wrap([]),
    )
    plugin._console_ok = lambda data=None, message=None: {"data": data, "message": message}
    plugin._console_error = lambda message, status_code=400: {
        "error": message,
        "status_code": status_code,
    }
    return plugin


def test_console_agent_transcript_route_returns_payload() -> None:
    records = [
        _control(CTRL_RUN_START, TASK_1),
        _message("user", "task", TASK_1),
        _control(CTRL_RUN_END, TASK_1),
    ]
    plugin = _make_route_plugin(records)

    response = asyncio.run(plugin.console_agent_transcript(AGENT_ID))

    runs = response["data"]["runs"]
    assert len(runs) == 1
    assert runs[0]["task_id"] == TASK_1
    assert runs[0]["user_text"] == "task"
    # 无 context 的路由测试桩 → 人格名回退为空串（前端据此省略标签）。
    assert response["data"]["mistress_name"] == ""


def test_console_agent_transcript_route_missing_agent_returns_404() -> None:
    plugin = _make_route_plugin([])

    async def load_agent(_agent_id):
        return None

    plugin.runtime_store.load_agent = load_agent

    response = asyncio.run(plugin.console_agent_transcript(AGENT_ID))

    assert response["status_code"] == 404


def test_console_agent_transcript_route_invalid_id_returns_400() -> None:
    plugin = _make_route_plugin([])

    async def load_agent(_agent_id):
        raise ValueError("非法 agent_id")

    plugin.runtime_store.load_agent = load_agent

    response = asyncio.run(plugin.console_agent_transcript("not-an-agent-id"))

    assert response["status_code"] == 400
    assert "非法 agent_id" in response["error"]


def test_resolve_mistress_name_uses_default_persona() -> None:
    plugin = object.__new__(MaidAgent)
    plugin.context = SimpleNamespace(
        persona_manager=SimpleNamespace(default_persona="沐雪")
    )
    assert plugin._resolve_mistress_name() == "沐雪"


def test_resolve_mistress_name_blank_when_unnamed_or_no_context() -> None:
    # 未命名占位 "default" → 空串。
    unnamed = object.__new__(MaidAgent)
    unnamed.context = SimpleNamespace(
        persona_manager=SimpleNamespace(default_persona="default")
    )
    assert unnamed._resolve_mistress_name() == ""
    # 无 context / 无 persona_manager → 空串，不抛异常。
    bare = object.__new__(MaidAgent)
    assert bare._resolve_mistress_name() == ""
