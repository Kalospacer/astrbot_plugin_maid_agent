"""Tests for the rewind mechanism (transcript folding + store API).

Rewind is append-only by design: nothing is deleted from ``transcript.jsonl``,
a marker is appended and the fold happens on read. These tests pin both halves —
the pure fold function and the store method that has to make ``resume`` see the
earlier state.
"""

from __future__ import annotations

import asyncio

from astrbot_plugin_maid_agent.runtime_store import (
    CTRL_REWIND,
    CTRL_RUN_END,
    CTRL_RUN_START,
    RuntimeStore,
    _read_jsonl,
    fold_rewound_records,
)


def _make_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_maid_agent.runtime_store.StarTools.get_data_dir",
        lambda _name: tmp_path,
    )
    return RuntimeStore(config=object())


def _start(task_id):
    return {"_control": True, "kind": CTRL_RUN_START, "task_id": task_id}


def _end(task_id):
    return {"_control": True, "kind": CTRL_RUN_END, "task_id": task_id}


def _rewind(task_id):
    return {"_control": True, "kind": CTRL_REWIND, "task_id": task_id}


def _msg(task_id, text):
    return {"role": "assistant", "task_id": task_id, "content": text}


def _texts(records):
    return [record["content"] for record in records if "content" in record]


# ------------------------------------------------------------------ fold 纯函数


def test_fold_without_markers_is_identity():
    records = [_start("aa"), _msg("aa", "one"), _end("aa")]
    kept, rewound = fold_rewound_records(records)
    assert kept == records
    assert rewound == []


def test_fold_drops_target_run_and_everything_after():
    records = [
        _start("aa"), _msg("aa", "one"), _end("aa"),
        _start("bb"), _msg("bb", "two"), _end("bb"),
        _start("cc"), _msg("cc", "three"), _end("cc"),
        _rewind("bb"),
    ]
    kept, rewound = fold_rewound_records(records)
    assert _texts(kept) == ["one"]
    assert rewound == ["bb", "cc"]
    # 标记本身不留在有效历史里
    assert not any(r.get("kind") == CTRL_REWIND for r in kept)


def test_fold_to_first_run_clears_everything():
    records = [_start("aa"), _msg("aa", "one"), _end("aa"), _rewind("aa")]
    kept, rewound = fold_rewound_records(records)
    assert kept == []
    assert rewound == ["aa"]


def test_fold_keeps_runs_appended_after_a_rewind():
    records = [
        _start("aa"), _msg("aa", "one"), _end("aa"),
        _start("bb"), _msg("bb", "two"), _end("bb"),
        _rewind("bb"),
        _start("cc"), _msg("cc", "three"), _end("cc"),
    ]
    kept, rewound = fold_rewound_records(records)
    assert _texts(kept) == ["one", "three"]
    assert rewound == ["bb"]


def test_fold_handles_nested_rewinds():
    records = [
        _start("aa"), _msg("aa", "one"), _end("aa"),
        _start("bb"), _msg("bb", "two"), _end("bb"),
        _rewind("bb"),
        _start("cc"), _msg("cc", "three"), _end("cc"),
        _rewind("cc"),
    ]
    kept, rewound = fold_rewound_records(records)
    assert _texts(kept) == ["one"]
    assert rewound == ["bb", "cc"]


def test_fold_ignores_marker_for_unknown_or_already_rewound_run():
    records = [
        _start("aa"), _msg("aa", "one"), _end("aa"),
        _rewind("aa"),
        _rewind("aa"),  # 重复：目标已不在有效历史里
        _rewind("ff"),  # 从未存在
    ]
    kept, rewound = fold_rewound_records(records)
    assert kept == []
    assert rewound == ["aa"]


def test_fold_keeps_records_before_the_first_run_start():
    records = [_msg("", "preamble"), _start("aa"), _msg("aa", "one"), _rewind("aa")]
    kept, rewound = fold_rewound_records(records)
    assert _texts(kept) == ["preamble"]
    assert rewound == ["aa"]


# ------------------------------------------------------------------- store API


def test_rewind_appends_marker_without_deleting_history(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)

    async def scenario():
        agent = await store.create_agent(
            unified_msg_origin="aiocqhttp:GroupMessage:g1",
            agent_name="butler",
            sender_id="u1",
        )
        aid = agent.agent_id
        for task_id, text in (("a" * 32, "one"), ("b" * 32, "two")):
            await store.append_control(aid, CTRL_RUN_START, {"task_id": task_id})
            await store.append_message(aid, _msg(task_id, text))
            await store.append_control(aid, CTRL_RUN_END, {"task_id": task_id})

        rewound = await store.rewind_to_run(aid, "b" * 32)
        raw = _read_jsonl(store._transcript_path(aid))
        contexts = await store.rebuild_contexts_for_resume(aid)
        return aid, rewound, raw, contexts

    _aid, rewound, raw, contexts = asyncio.run(scenario())

    assert rewound == ["b" * 32]
    # 磁盘上一条都没少，只是多了一条标记
    assert _texts(raw) == ["one", "two"]
    assert sum(1 for r in raw if r.get("kind") == CTRL_REWIND) == 1
    # resume 上下文里第二轮已经消失
    assert _texts(contexts) == ["one"]


def test_rewind_rejects_run_outside_effective_history(tmp_path, monkeypatch):
    store = _make_store(tmp_path, monkeypatch)

    async def scenario():
        agent = await store.create_agent(
            unified_msg_origin="aiocqhttp:GroupMessage:g1",
            agent_name="butler",
            sender_id="u1",
        )
        aid = agent.agent_id
        await store.append_control(aid, CTRL_RUN_START, {"task_id": "a" * 32})
        await store.append_message(aid, _msg("a" * 32, "one"))
        await store.rewind_to_run(aid, "a" * 32)

        errors = []
        for task_id in ("a" * 32, "c" * 32):
            try:
                await store.rewind_to_run(aid, task_id)
            except ValueError as exc:
                errors.append(str(exc))
        return errors

    errors = asyncio.run(scenario())
    assert len(errors) == 2
    assert all("无法回溯" in message for message in errors)
