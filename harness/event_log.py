"""会话事件日志与元数据，存在 SQLite 的 sessions / session_events 表里。

每个会话有不可变的 header、可变的 meta 和一串事件；事件 seq 从 0 连续递增，
只能经由 append() 分配。
"""

from __future__ import annotations

import asyncio

from .contracts import KNOWN_EVENT_TYPES, SESSION_FORMAT_VERSION, make_event
from .db import Database, dump, load


class SessionLogError(RuntimeError):
    pass


class SessionLog:
    """单个会话的事件日志。追加必须持有该会话的锁（进程内）。"""

    def __init__(self, db: Database, session_id: str):
        self.db = db
        self.session_id = session_id
        self.lock = asyncio.Lock()
        self._events: list[dict] | None = None

    def exists(self) -> bool:
        return self.db.execute("SELECT 1 FROM sessions WHERE id = ?", (self.session_id,)).fetchone() is not None

    def create(self, header: dict) -> None:
        if self.exists():
            raise SessionLogError(f"session 已存在: {self.session_id}")
        payload = {"version": SESSION_FORMAT_VERSION, "id": self.session_id, **header}
        self.db.execute("INSERT INTO sessions (id, header, meta) VALUES (?, ?, '{}')", (self.session_id, dump(payload)))
        self._events = []

    def load_header(self) -> dict | None:
        row = self.db.execute("SELECT header FROM sessions WHERE id = ?", (self.session_id,)).fetchone()
        return load(row[0]) if row else None

    def save_header(self, header: dict) -> None:
        self.db.execute("UPDATE sessions SET header = ? WHERE id = ?", (dump(header), self.session_id))

    def load_meta(self) -> dict:
        row = self.db.execute("SELECT meta FROM sessions WHERE id = ?", (self.session_id,)).fetchone()
        return load(row[0]) if row else {}

    def save_meta(self, meta: dict) -> None:
        self.db.execute("UPDATE sessions SET meta = ? WHERE id = ?", (dump(meta), self.session_id))

    def update_meta(self, **fields) -> dict:
        meta = self.load_meta()
        meta.update(fields)
        self.save_meta(meta)
        return meta

    def _load_events(self) -> list[dict]:
        if self._events is None:
            rows = self.db.execute(
                "SELECT value FROM session_events WHERE session_id = ? ORDER BY seq", (self.session_id,)
            ).fetchall()
            self._events = [load(row[0]) for row in rows]
        return self._events

    def read_events(self) -> list[dict]:
        return list(self._load_events())

    @property
    def last_seq(self) -> int:
        return len(self._load_events()) - 1

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
        if event_type not in KNOWN_EVENT_TYPES and not ignorable:
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
        self.db.execute(
            "INSERT INTO session_events (session_id, seq, value) VALUES (?, ?, ?)",
            (self.session_id, event["seq"], dump(event)),
        )
        events.append(event)
        return event

    def delete(self) -> None:
        """删除会话的日志、元数据与上下文快照。"""
        with self.db.transaction():
            for table in ("session_events", "contexts", "context_checkpoints"):
                self.db.execute(f"DELETE FROM {table} WHERE session_id = ?", (self.session_id,))
            self.db.execute("DELETE FROM sessions WHERE id = ?", (self.session_id,))
        self._events = None
