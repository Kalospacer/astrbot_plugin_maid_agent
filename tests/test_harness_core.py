"""核心层测试：事件日志、投影、历史分页、rewind。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrbot_plugin_maid_agent.harness import contracts as c
from astrbot_plugin_maid_agent.harness.event_log import SessionLog
from astrbot_plugin_maid_agent.harness.history import derive_surface, history_page, in_flight_partial, visible_events
from astrbot_plugin_maid_agent.harness.projections import ProjectionRegistry


@pytest.fixture()
def log(tmp_path: Path) -> SessionLog:
    log = SessionLog(tmp_path / "sessions", "a" * 32)
    log.create({"createdAt": 1000})
    return log


def _emit_turn(log: SessionLog, turn: int, *, text: str = "回复", chunks: list[str] | None = None):
    """写入一个最小 turn：start/chunks/message/end。"""
    log.append("turn/start", {"turn": turn})
    log.append("user/message", c.user_message([c.text_block(f"问{turn}")]), source_event_seqs=[])
    log.append("step/start", {"turn": turn, "step": 1})
    for i, chunk in enumerate(chunks or ["回", "复"]):
        log.append("assistant/chunk", {"turn": turn, "step": 1, "chunk": c.text_delta_chunk(0, chunk)})
    log.append(
        "assistant/message",
        {
            "turn": turn,
            "step": 1,
            "message": c.assistant_message([c.text_block(text)], "p", "m"),
            "usage": {"inputTokens": 10, "outputTokens": 5},
        },
        source_event_seqs=[],
    )
    log.append("step/end", {"turn": turn, "step": 1})
    log.append("turn/end", {"turn": turn, "reason": c.reason_completed()})


class TestEventLog:
    def test_seq_contiguous_from_zero(self, log: SessionLog):
        for i in range(5):
            event = log.append("turn/start", {"turn": i})
            assert event["seq"] == i
        assert log.last_seq == 4

    def test_surface_fields_only_on_surface(self, log: SessionLog):
        log.append("turn/start", {"turn": 0})
        assert "surfaceOp" not in log.read_events()[0]
        msg = log.append("user/message", c.user_message([c.text_block("hi")]))
        assert msg["surfaceOp"] == "append"

    def test_unknown_type_requires_ignorable(self, log: SessionLog):
        from astrbot_plugin_maid_agent.harness.event_log import SessionLogError

        with pytest.raises(SessionLogError):
            log.append("wat/unknown", {})
        log.append("wat/unknown", {}, ignorable=True)

    def test_reload_from_disk(self, log: SessionLog, tmp_path: Path):
        _emit_turn(log, 0)
        log.invalidate_cache()
        fresh = SessionLog(tmp_path / "sessions", "a" * 32)
        assert fresh.last_seq == log.last_seq
        assert [e["type"] for e in fresh.read_events()] == [e["type"] for e in log.read_events()]

    def test_truncated_tail_line_dropped(self, log: SessionLog, tmp_path: Path):
        _emit_turn(log, 0)
        with open(log.events_path, "a", encoding="utf-8") as fh:
            fh.write('{"type": "turn/st')  # 损坏尾行
        log.invalidate_cache()
        events = log.read_events()
        assert events[-1]["type"] == "turn/end"
        assert log.append("turn/start", {"turn": 1})["seq"] == len(events)


class TestProjections:
    def test_folds(self, log: SessionLog):
        _emit_turn(log, 0, text="答案", chunks=["答", "案"])
        _emit_turn(log, 1)
        registry = ProjectionRegistry()
        result = registry.compute(log.session_id, log.read_events())
        values = result["values"]
        assert values["title"] is None
        assert values["sessionStats"]["turns"] == 2
        assert values["sessionStats"]["steps"] == 2
        assert values["tokenUsage"]["uncachedInputTokens"] == 20
        assert values["tokenUsage"]["outputTokens"] == 10
        assert result["asOfSeq"] == log.last_seq

    def test_title_latest_wins(self, log: SessionLog):
        _emit_turn(log, 0)
        log.append("session/title", {"title": "旧标题", "source": {"kind": "auto"}})
        log.append("session/title", {"title": "新标题", "source": {"kind": "user"}})
        values = ProjectionRegistry().compute(log.session_id, log.read_events())["values"]
        assert values["title"] == "新标题"


class TestHistoryPaging:
    def test_blocks_never_cut_mid_message(self, log: SessionLog):
        for turn in range(5):
            _emit_turn(log, turn)
        page = history_page(log.read_events(), max_messages=2)
        # 每条消息块：user/message 独占一块（turn/start..user/message），assistant 一块
        assert page["has_more"] is True
        types = [e["type"] for e in page["events"]]
        assert types[0] == "turn/start"
        assert types[-1] == "turn/end"
        assert "assistant/chunk" in types

    def test_tail_partial(self, log: SessionLog):
        _emit_turn(log, 0)
        log.append("turn/start", {"turn": 1})
        log.append("user/message", c.user_message([c.text_block("q")]))
        log.append("step/start", {"turn": 1, "step": 1})
        log.append("assistant/chunk", {"turn": 1, "step": 1, "chunk": c.text_delta_chunk(0, "部")})
        partial = in_flight_partial(log.read_events())
        assert [e["type"] for e in partial] == ["step/start", "assistant/chunk"]
        page = history_page(log.read_events())
        assert page["partial_from"] is not None

    def test_before_seq_page(self, log: SessionLog):
        for turn in range(4):
            _emit_turn(log, turn)
        events = log.read_events()
        surface_seqs = [e["seq"] for e in derive_surface(events)]
        before = surface_seqs[6]  # turn3 的 user/message
        page = history_page(events, before_seq=before, max_messages=2)
        page_events = page["events"]
        page_surface = [e for e in page_events if e["type"] in c.SURFACE_EVENT_TYPES]
        assert all(e["seq"] < before for e in page_surface)
        assert page["has_more"] is True
        assert len(page_surface) == 2  # 恰好 turn2 的两条消息


class TestRewind:
    def test_visibility(self, log: SessionLog):
        _emit_turn(log, 0)
        _emit_turn(log, 1)
        _emit_turn(log, 2)
        events = log.read_events()
        surface = derive_surface(events)
        target_seq = surface[2]["seq"]  # rewind 第二个 turn 的 user/message
        log.append("maid/rewind", {"atSeq": target_seq}, ignorable=True)
        visible = visible_events(log.read_events())
        surface_after = derive_surface(visible)
        assert surface_after == surface[:2]
        # rewind 之后的新事件可见（turn0 的 2 条 + turn3 的 2 条）
        _emit_turn(log, 3)
        assert len(derive_surface(log.read_events())) == 4
