"""append-only 事件日志（对应 session-persistence-jsonl）。

目录形状::

    sessions/<sessionId>/header.json    # 不可变存储元数据
    sessions/<sessionId>/meta.json      # 可变侧车：ops 元数据（umo/agent/pinned title...）
    sessions/<sessionId>/events.jsonl   # 事件日志，seq 从 0 单调连续

seq 连续性是硬约束：任何写入都经由 append() 分配。
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .contracts import KNOWN_EVENT_TYPES, SESSION_FORMAT_VERSION, make_event


class SessionLogError(RuntimeError):
    pass


def _read_json(path: Path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


class SessionLog:
    """单个会话的事件日志。追加必须持有该会话的锁（进程内）。"""

    def __init__(self, root: Path, session_id: str):
        self.session_id = session_id
        self.dir = root / session_id
        self.header_path = self.dir / "header.json"
        self.meta_path = self.dir / "meta.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock = asyncio.Lock()
        self._events: list[dict] | None = None


    def exists(self) -> bool:
        return self.header_path.exists()

    def create(self, header: dict) -> None:
        if self.exists():
            raise SessionLogError(f"session 已存在: {self.session_id}")
        payload = {"version": SESSION_FORMAT_VERSION, "id": self.session_id, **header}
        self.dir.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.header_path, payload)
        self.events_path.touch()


    def load_header(self) -> dict | None:
        return _read_json(self.header_path)

    def load_meta(self) -> dict:
        return _read_json(self.meta_path) or {}

    def save_meta(self, meta: dict) -> None:
        _write_json_atomic(self.meta_path, meta)

    def update_meta(self, **fields) -> dict:
        meta = self.load_meta()
        meta.update(fields)
        self.save_meta(meta)
        return meta


    def _load_events(self) -> list[dict]:
        if self._events is not None:
            return self._events
        events: list[dict] = []
        try:
            with open(self.events_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        break
        except FileNotFoundError:
            pass
        self._events = events
        return events

    def invalidate_cache(self) -> None:
        self._events = None

    def read_events(self) -> list[dict]:
        return list(self._load_events())

    @property
    def last_seq(self) -> int:
        events = self._load_events()
        return len(events) - 1


    def append(
        self,
        event_type: str,
        data: dict,
        *,
        ignorable: bool = False,
        surface_op: str | None = "append",
        source_event_seqs: list[int] | None = None,
        time_ms: int | None = None,
    ) -> dict:
        """同步追加（调用方负责持锁）。返回带 seq 的完整事件。"""
        if event_type not in KNOWN_EVENT_TYPES:
            if not ignorable:
                raise SessionLogError(f"未知且不可忽略的事件类型: {event_type}")
        events = self._load_events()
        event = make_event(
            event_type,
            len(events),
            data,
            ignorable=ignorable,
            surface_op=surface_op,
            source_event_seqs=source_event_seqs,
            time_ms=time_ms,
        )
        with open(self.events_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        events.append(event)
        return event

    def append_many(self, items: list[dict]) -> list[dict]:
        """批量写入裸 (type, data) 元组列表，保持 seq 连续。"""
        out = []
        for item in items:
            out.append(self.append(item["type"], item.get("data", {}), **item.get("extra", {})))
        return out
