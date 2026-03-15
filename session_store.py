"""
大小姐管家模式插件 - Session 存储层
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .constants import ACTIVE_SESSION_INDEX_KEY, PLUGIN_DATA_DIR_NAME

if TYPE_CHECKING:
    from .config import MaidModeConfig

UTC = timezone.utc


def _utcnow() -> datetime:
    return datetime.now(UTC)


LOCK_ENTRY_TTL = timedelta(hours=1)


@dataclass(slots=True)
class _UmoLockEntry:
    lock: asyncio.Lock
    ref_count: int
    last_used_at: datetime


@dataclass(slots=True)
class MaidAgentSession:
    session_id: str
    unified_msg_origin: str
    agent_name: str
    status: str
    messages: list[dict[str, Any]]
    created_at: str
    updated_at: str
    last_maid_request: str
    last_agent_result: str

    @classmethod
    def create(cls, unified_msg_origin: str, agent_name: str) -> MaidAgentSession:
        now = _utcnow().isoformat()
        return cls(
            session_id=uuid.uuid4().hex,
            unified_msg_origin=unified_msg_origin,
            agent_name=agent_name,
            status="active",
            messages=[],
            created_at=now,
            updated_at=now,
            last_maid_request="",
            last_agent_result="",
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaidAgentSession:
        raw_messages = data.get("messages", []) or []
        messages = [item for item in raw_messages if isinstance(item, dict)]
        return cls(
            session_id=str(data.get("session_id", "")),
            unified_msg_origin=str(data.get("unified_msg_origin", "")),
            agent_name=str(data.get("agent_name", "")),
            status=str(data.get("status", "active")),
            messages=messages,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            last_maid_request=str(data.get("last_maid_request", "")),
            last_agent_result=str(data.get("last_agent_result", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "unified_msg_origin": self.unified_msg_origin,
            "agent_name": self.agent_name,
            "status": self.status,
            "messages": self.messages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_maid_request": self.last_maid_request,
            "last_agent_result": self.last_agent_result,
        }

    def touch(self) -> None:
        self.updated_at = _utcnow().isoformat()

    def is_expired(self, timeout_minutes: int) -> bool:
        if timeout_minutes <= 0:
            return False
        try:
            updated_at = datetime.fromisoformat(self.updated_at)
        except ValueError:
            return True
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return _utcnow() - updated_at > timedelta(minutes=timeout_minutes)


class MaidSessionStore:
    def __init__(self, plugin, config: MaidModeConfig) -> None:
        self.plugin = plugin
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME)
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._umo_locks: dict[str, _UmoLockEntry] = {}
        self._umo_locks_guard = asyncio.Lock()
        self._active_index_lock = asyncio.Lock()

    def _session_path(self, session_id: str) -> Path:
        normalized = session_id.strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{32}", normalized):
            raise ValueError(f"非法 session_id: {session_id!r}")
        path = (self.sessions_dir / f"{normalized}.json").resolve()
        sessions_root = self.sessions_dir.resolve()
        if path.parent != sessions_root:
            raise ValueError(f"session 路径越界: {session_id!r}")
        return path

    def _prune_stale_umo_locks_unlocked(self, now: datetime) -> None:
        stale_keys = [
            key
            for key, entry in self._umo_locks.items()
            if entry.ref_count == 0 and now - entry.last_used_at > LOCK_ENTRY_TTL
        ]
        for key in stale_keys:
            self._umo_locks.pop(key, None)

    async def _acquire_umo_lock_entry(self, unified_msg_origin: str) -> _UmoLockEntry:
        now = _utcnow()
        async with self._umo_locks_guard:
            self._prune_stale_umo_locks_unlocked(now)
            entry = self._umo_locks.get(unified_msg_origin)
            if entry is None:
                entry = _UmoLockEntry(lock=asyncio.Lock(), ref_count=0, last_used_at=now)
                self._umo_locks[unified_msg_origin] = entry
            entry.ref_count += 1
            entry.last_used_at = now
            return entry

    async def _release_umo_lock_entry(
        self,
        unified_msg_origin: str,
        entry: _UmoLockEntry,
    ) -> None:
        now = _utcnow()
        async with self._umo_locks_guard:
            current = self._umo_locks.get(unified_msg_origin)
            if current is entry:
                current.ref_count = max(0, current.ref_count - 1)
                current.last_used_at = now
            self._prune_stale_umo_locks_unlocked(now)

    @asynccontextmanager
    async def _hold_umo_lock(self, unified_msg_origin: str):
        entry = await self._acquire_umo_lock_entry(unified_msg_origin)
        await entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            await self._release_umo_lock_entry(unified_msg_origin, entry)

    async def _load_active_index(self) -> dict[str, str]:
        stored = await self.plugin.get_kv_data(ACTIVE_SESSION_INDEX_KEY, {})
        return stored if isinstance(stored, dict) else {}

    async def _save_active_index(self, index: dict[str, str]) -> None:
        await self.plugin.put_kv_data(ACTIVE_SESSION_INDEX_KEY, index)

    async def _set_active_session_id(self, unified_msg_origin: str, session_id: str) -> None:
        async with self._active_index_lock:
            index = await self._load_active_index()
            index[unified_msg_origin] = session_id
            await self._save_active_index(index)

    async def _clear_active_session_id(self, unified_msg_origin: str) -> None:
        async with self._active_index_lock:
            index = await self._load_active_index()
            if unified_msg_origin in index:
                index.pop(unified_msg_origin, None)
                await self._save_active_index(index)

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        temp_path = path.with_suffix(f"{path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None

    async def save_session(self, session: MaidAgentSession) -> None:
        session.touch()
        try:
            await asyncio.to_thread(
                self._write_json_atomic,
                self._session_path(session.session_id),
                session.to_dict(),
            )
        except Exception as exc:
            logger.error("[大小姐模式] 写入 session 文件失败: %s", exc, exc_info=True)
            raise

    async def load_session(self, session_id: str) -> MaidAgentSession | None:
        try:
            path = self._session_path(session_id)
        except (TypeError, AttributeError, ValueError) as exc:
            logger.error("[大小姐模式] session_id 校验失败: %s", exc)
            return None
        if not await asyncio.to_thread(path.exists):
            return None
        try:
            data = await asyncio.to_thread(self._read_json, path)
            if data is None:
                return None
            return MaidAgentSession.from_dict(data)
        except Exception as exc:
            logger.error("[大小姐模式] 读取 session 文件失败: %s", exc, exc_info=True)
            return None

    async def _get_active_session_unlocked(
        self,
        unified_msg_origin: str,
    ) -> MaidAgentSession | None:
        if not self.config.session_enabled:
            return None

        index = await self._load_active_index()
        session_id = index.get(unified_msg_origin)
        if not session_id:
            return None

        session = await self.load_session(session_id)
        if session is None:
            await self._clear_active_session_id(unified_msg_origin)
            return None

        if session.status != "active":
            await self._clear_active_session_id(unified_msg_origin)
            return None

        if session.is_expired(self.config.session_timeout_minutes):
            session.status = "expired"
            await self.save_session(session)
            await self._clear_active_session_id(unified_msg_origin)
            logger.info(
                "[大小姐模式] 管家 session 已超时失效: umo=%s session_id=%s",
                unified_msg_origin,
                session.session_id,
            )
            return None

        return session

    async def get_active_session(self, unified_msg_origin: str) -> MaidAgentSession | None:
        async with self._hold_umo_lock(unified_msg_origin):
            return await self._get_active_session_unlocked(unified_msg_origin)

    async def get_or_create_active_session(
        self,
        unified_msg_origin: str,
        agent_name: str,
    ) -> tuple[MaidAgentSession, bool]:
        async with self._hold_umo_lock(unified_msg_origin):
            session = await self._get_active_session_unlocked(unified_msg_origin)
            if session is not None:
                if session.agent_name.strip().casefold() != agent_name.strip().casefold():
                    session.status = "expired"
                    await self.save_session(session)
                    await self._clear_active_session_id(unified_msg_origin)
                    logger.info(
                        "[大小姐模式] 检测到跨 agent session 复用，已关闭旧 session: umo=%s old_session_id=%s old_agent=%s new_agent=%s",
                        unified_msg_origin,
                        session.session_id,
                        session.agent_name,
                        agent_name,
                    )
                else:
                    return session, True

            session = MaidAgentSession.create(unified_msg_origin, agent_name)
            await self.save_session(session)
            await self._set_active_session_id(unified_msg_origin, session.session_id)
            logger.info(
                "[大小姐模式] 已创建新的管家 session: umo=%s session_id=%s agent=%s",
                unified_msg_origin,
                session.session_id,
                agent_name,
            )
            return session, False

    async def close_active_session(
        self,
        unified_msg_origin: str,
        status: str = "done",
    ) -> MaidAgentSession | None:
        async with self._hold_umo_lock(unified_msg_origin):
            session = await self._get_active_session_unlocked(unified_msg_origin)
            if session is None:
                return None

            session.status = status
            await self.save_session(session)
            await self._clear_active_session_id(unified_msg_origin)
            logger.info(
                "[大小姐模式] 已关闭管家 session: umo=%s session_id=%s status=%s",
                unified_msg_origin,
                session.session_id,
                status,
            )
            return session
