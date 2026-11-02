"""管家会话实际运行消息的持久化快照，存在 contexts / context_checkpoints 表里。"""

from __future__ import annotations

import base64
import mimetypes
from copy import deepcopy
from pathlib import Path

from .db import Database, dump, load


def freeze_messages(messages: list, *, exclude_system: bool = False) -> list[dict]:
    """复制运行消息，不改动主 runner；本地媒体转为稳定 data URI。"""
    copied = []
    for message in messages:
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role == "_checkpoint" or (exclude_system and role == "system"):
            continue
        copied.append(deepcopy(message if isinstance(message, dict) else message.model_dump()))
    _freeze_media(copied)
    return copied


def _freeze_media(value) -> None:
    if isinstance(value, list):
        for item in value:
            _freeze_media(item)
    elif isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str) and not url.startswith(("http://", "https://", "data:")):
            local = Path(url.removeprefix("file://"))
            if local.is_file():
                media_type = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
                value["url"] = f"data:{media_type};base64," + base64.b64encode(local.read_bytes()).decode()
        for item in value.values():
            _freeze_media(item)


class ContextState:
    def __init__(self, db: Database, session_id: str):
        self.db = db
        self.session_id = session_id
        # 一轮里每步都会读写这份快照，缓存避免重复读库；实例之间仍以数据库为准。
        self._state: dict | None = None

    def load(self) -> dict:
        if self._state is None:
            row = self.db.execute("SELECT value FROM contexts WHERE session_id = ?", (self.session_id,)).fetchone()
            self._state = load(row[0]) if row else {"messages": [], "systemPrompt": ""}
        return self._state

    def _save(self, state: dict) -> None:
        self._state = state
        self.db.execute(
            "INSERT INTO contexts (session_id, value) VALUES (?, ?) "
            "ON CONFLICT (session_id) DO UPDATE SET value = excluded.value",
            (self.session_id, dump(state)),
        )

    def messages(self) -> list[dict]:
        """下一轮 runner 的初始上下文。"""
        return deepcopy(self.load()["messages"])

    def capture(self, messages: list, system_prompt: str) -> None:
        actual = freeze_messages(messages)
        # runner 在头部加入子人格；每轮从配置读取，避免恢复时重复加入。
        if actual and actual[0].get("role") == "system":
            actual = actual[1:]
        state = self.load()
        if state["messages"] == actual and state["systemPrompt"] == system_prompt:
            # 这一步没有新增内容，不必重写整份快照。
            return
        self._save({"messages": actual, "systemPrompt": system_prompt})

    def checkpoint(self, turn: int) -> None:
        self.db.execute(
            "INSERT INTO context_checkpoints (session_id, turn, value) VALUES (?, ?, ?) "
            "ON CONFLICT (session_id, turn) DO UPDATE SET value = excluded.value",
            (self.session_id, turn, dump(self.load())),
        )

    def has_checkpoint(self, turn: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM context_checkpoints WHERE session_id = ? AND turn = ?", (self.session_id, turn)
        ).fetchone()
        return row is not None

    def fork_state(self, turn: int) -> dict:
        row = self.db.execute(
            "SELECT value FROM context_checkpoints WHERE session_id = ? AND turn = ?", (self.session_id, turn)
        ).fetchone()
        if row is None:
            raise ValueError("目标轮次没有上下文快照，不能从展示日志恢复。")
        return load(row[0])

    def seed(self, state: dict) -> None:
        self._save(deepcopy(state))
