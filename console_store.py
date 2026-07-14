"""大小姐管家模式插件 - WebUI 控制台事件存储。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api.star import StarTools

from .constants import PLUGIN_DATA_DIR_NAME

UTC = timezone.utc


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _dump_json(data: Any) -> str:
    try:
        return json.dumps(data if data is not None else {}, ensure_ascii=False, default=str)
    except Exception:
        return json.dumps({"repr": repr(data)}, ensure_ascii=False)


def _load_json(raw: str | None) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


@dataclass(slots=True)
class ConsoleTaskPatch:
    task_id: str
    kind: str = "single"
    source: str = "chat"
    unified_msg_origin: str = ""
    sender_id: str = ""
    agent_name: str = ""
    status: str = "queued"
    title: str = ""
    request_text: str = ""
    parent_task_id: str = ""
    meta: dict[str, Any] | None = None
    # 1.3.0 runtime fields (stored inside meta_json for backward compatibility).
    agent_id: str = ""
    run_mode: str = ""  # foreground | background
    background_reason: str = ""
    notification_id: str = ""


class MaidConsoleEventStore:
    """SQLite backed audit/event stream for the plugin WebUI."""

    def __init__(self) -> None:
        data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME)
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "maid_console.sqlite3"
        self._write_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._subscriber_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._init_db)

    async def reconcile_incomplete_tasks(self) -> list[str]:
        """Close persisted tasks whose in-memory runner disappeared after restart."""
        async with self._write_lock:
            return await asyncio.to_thread(self._reconcile_incomplete_tasks, _utcnow())

    async def close(self) -> None:
        async with self._subscriber_lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait({"type": "closed", "created_at": _utcnow()})

    async def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        async with self._subscriber_lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._subscriber_lock:
            self._subscribers.discard(queue)

    async def ensure_task(self, patch: ConsoleTaskPatch) -> dict[str, Any]:
        now = _utcnow()
        meta = dict(patch.meta or {})
        # 1.3.0 runtime fields are merged into meta_json so the SQLite schema
        # stays backward compatible with 1.2.0 audits.
        if patch.agent_id:
            meta.setdefault("agent_id", patch.agent_id)
        if patch.run_mode:
            meta.setdefault("run_mode", patch.run_mode)
        if patch.background_reason:
            meta.setdefault("background_reason", patch.background_reason)
        if patch.notification_id:
            meta.setdefault("notification_id", patch.notification_id)
        payload = {
            "task_id": patch.task_id,
            "parent_task_id": patch.parent_task_id,
            "kind": patch.kind,
            "source": patch.source,
            "unified_msg_origin": patch.unified_msg_origin,
            "sender_id": patch.sender_id,
            "agent_name": patch.agent_name,
            "status": patch.status,
            "title": patch.title or patch.request_text[:80],
            "request_text": patch.request_text,
            "meta_json": _dump_json(meta),
            "created_at": now,
            "updated_at": now,
            "completed_at": (
                now
                if patch.status
                in {"done", "error", "completed", "failed", "stopped", "interrupted"}
                else ""
            ),
        }
        async with self._write_lock:
            task = await asyncio.to_thread(self._upsert_task, payload)
        await self._publish({"type": "task", "task": task})
        return task

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utcnow()
        async with self._write_lock:
            task = await asyncio.to_thread(
                self._update_task_status,
                task_id,
                status,
                now,
                _dump_json(meta) if meta is not None else None,
            )
        if task is not None:
            await self._publish({"type": "task", "task": task})
        return task

    async def record_event(
        self,
        *,
        task_id: str,
        event_type: str,
        title: str,
        message: str = "",
        source: str = "system",
        status: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _utcnow()
        event_payload = {
            "task_id": task_id,
            "event_type": event_type,
            "title": title,
            "message": message,
            "source": source,
            "status": status,
            "payload_json": _dump_json(payload or {}),
            "created_at": now,
        }
        async with self._write_lock:
            event = await asyncio.to_thread(self._insert_event, event_payload)
        await self._publish({"type": "event", "event": event})
        return event

    async def record_action(
        self,
        *,
        task_id: str,
        action: str,
        source: str = "dashboard",
        payload: dict[str, Any] | None = None,
        result_text: str = "",
        status: str = "ok",
    ) -> dict[str, Any]:
        now = _utcnow()
        action_payload = {
            "task_id": task_id,
            "action": action,
            "source": source,
            "payload_json": _dump_json(payload or {}),
            "result_text": result_text,
            "status": status,
            "created_at": now,
        }
        async with self._write_lock:
            action_row = await asyncio.to_thread(self._insert_action, action_payload)
        await self._publish({"type": "action", "action": action_row})
        return action_row

    async def list_tasks(
        self,
        *,
        limit: int = 80,
        status: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        limit = min(max(limit, 1), 300)
        return await asyncio.to_thread(self._list_tasks, limit, status, query)

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_task, task_id)

    async def list_agents(self, unified_msg_origin: str = "") -> list[dict[str, Any]]:
        """Aggregate distinct agent_id -> latest task summary from the audit store.

        1.3.0 runtime agents are surfaced by scanning task meta_json. The SQLite
        store remains an audit layer (not the runtime source of truth)."""
        return await asyncio.to_thread(self._list_agents, unified_msg_origin)

    async def get_agent_runs(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all audited runs/tasks for a given 1.3.0 agent_id."""
        return await asyncio.to_thread(self._get_agent_runs, agent_id)

    async def get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_task_events, task_id)

    async def get_task_actions(self, task_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._get_task_actions, task_id)

    async def get_overview(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_overview)

    async def export_history(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._export_history)

    async def clear_history(self) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._clear_history)
        await self._publish({"type": "reset", "created_at": _utcnow()})

    async def delete_task(self, task_id: str) -> None:
        async with self._write_lock:
            await asyncio.to_thread(self._delete_task, task_id)
        await self._publish({"type": "reset", "created_at": _utcnow()})

    async def update_task_meta(
        self,
        task_id: str,
        title: str | None = None,
        meta_update: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = _utcnow()
        async with self._write_lock:
            task = await asyncio.to_thread(
                self._update_task_meta,
                task_id,
                title,
                meta_update,
                now,
            )
        if task:
            await self._publish({"type": "task", "task": task})
        return task

    async def _publish(self, item: dict[str, Any]) -> None:
        item.setdefault("event_id", uuid.uuid4().hex)
        item.setdefault("created_at", _utcnow())
        async with self._subscriber_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(item)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    parent_task_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'single',
                    source TEXT NOT NULL DEFAULT 'chat',
                    unified_msg_origin TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    agent_name TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    title TEXT NOT NULL DEFAULT '',
                    request_text TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'system',
                    status TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT 'dashboard',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    result_text TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'ok',
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_task ON task_actions(task_id, id)")

    def _reconcile_incomplete_tasks(self, now: str) -> list[str]:
        active_statuses = ("queued", "running", "stopping")
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT task_id, meta_json FROM tasks WHERE status IN (?, ?, ?)",
                active_statuses,
            ).fetchall()
            task_ids: list[str] = []
            for row in rows:
                task_id = str(row["task_id"])
                meta = _load_json(row["meta_json"])
                next_meta = meta if isinstance(meta, dict) else {}
                next_meta.update(
                    {
                        "recovered_after_restart": True,
                        "interruption_reason": "plugin restarted before task completion",
                    }
                )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'stopped', updated_at = ?, completed_at = ?, meta_json = ?
                    WHERE task_id = ?
                    """,
                    (now, now, _dump_json(next_meta), task_id),
                )
                conn.execute(
                    """
                    INSERT INTO task_events (
                        task_id, event_type, title, message, source, status,
                        payload_json, created_at
                    ) VALUES (?, 'interrupted', ?, ?, 'system', 'stopped', ?, ?)
                    """,
                    (
                        task_id,
                        "任务因插件重启而中断",
                        "运行中的内存执行器已不存在，任务状态已收敛为 stopped。",
                        _dump_json({"recovered_after_restart": True}),
                        now,
                    ),
                )
                task_ids.append(task_id)
        return task_ids

    def _row_to_task(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data = dict(row)
        data["meta"] = _load_json(data.pop("meta_json", "{}"))
        return data

    def _row_to_event(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = _load_json(data.pop("payload_json", "{}"))
        return data

    def _row_to_action(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["payload"] = _load_json(data.pop("payload_json", "{}"))
        return data

    def _upsert_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            existing_row = conn.execute(
                "SELECT meta_json FROM tasks WHERE task_id = ?",
                (payload["task_id"],),
            ).fetchone()
            if existing_row is not None:
                existing_meta = _load_json(existing_row["meta_json"])
                next_meta = existing_meta if isinstance(existing_meta, dict) else {}
                patch_meta = _load_json(payload.get("meta_json"))
                if isinstance(patch_meta, dict):
                    next_meta.update(patch_meta)
                payload = {**payload, "meta_json": _dump_json(next_meta)}
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, parent_task_id, kind, source, unified_msg_origin,
                    sender_id, agent_name, status, title, request_text, meta_json,
                    created_at, updated_at, completed_at
                ) VALUES (
                    :task_id, :parent_task_id, :kind, :source, :unified_msg_origin,
                    :sender_id, :agent_name, :status, :title, :request_text,
                    :meta_json, :created_at, :updated_at, :completed_at
                )
                ON CONFLICT(task_id) DO UPDATE SET
                    parent_task_id = CASE
                        WHEN excluded.parent_task_id != '' THEN excluded.parent_task_id
                        ELSE tasks.parent_task_id
                    END,
                    kind = excluded.kind,
                    source = excluded.source,
                    unified_msg_origin = CASE
                        WHEN excluded.unified_msg_origin != '' THEN excluded.unified_msg_origin
                        ELSE tasks.unified_msg_origin
                    END,
                    sender_id = CASE
                        WHEN excluded.sender_id != '' THEN excluded.sender_id
                        ELSE tasks.sender_id
                    END,
                    agent_name = CASE
                        WHEN excluded.agent_name != '' THEN excluded.agent_name
                        ELSE tasks.agent_name
                    END,
                    status = excluded.status,
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE tasks.title END,
                    request_text = CASE
                        WHEN excluded.request_text != '' THEN excluded.request_text
                        ELSE tasks.request_text
                    END,
                    meta_json = excluded.meta_json,
                    updated_at = excluded.updated_at,
                    completed_at = CASE
                        WHEN excluded.completed_at != '' THEN excluded.completed_at
                        ELSE tasks.completed_at
                    END
                """,
                payload,
            )
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (payload["task_id"],),
            ).fetchone()
        task = self._row_to_task(row)
        assert task is not None
        return task

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        now: str,
        meta_json: str | None,
    ) -> dict[str, Any] | None:
        completed_at = (
            now
            if status
            in {
                "done",
                "error",
                "completed",
                "failed",
                "stopped",
                "interrupted",
                "partial_done",
            }
            else ""
        )
        with self._connect() as conn:
            if meta_json is None:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?,
                        completed_at = CASE WHEN ? != '' THEN ? ELSE completed_at END
                    WHERE task_id = ?
                    """,
                    (status, now, completed_at, completed_at, task_id),
                )
            else:
                existing_row = conn.execute(
                    "SELECT meta_json FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                existing_meta = _load_json(existing_row["meta_json"]) if existing_row else {}
                next_meta = existing_meta if isinstance(existing_meta, dict) else {}
                patch_meta = _load_json(meta_json)
                if isinstance(patch_meta, dict):
                    next_meta.update(patch_meta)
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, updated_at = ?, meta_json = ?,
                        completed_at = CASE WHEN ? != '' THEN ? ELSE completed_at END
                    WHERE task_id = ?
                    """,
                    (status, now, _dump_json(next_meta), completed_at, completed_at, task_id),
                )
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row_to_task(row)

    def _insert_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_events (
                    task_id, event_type, title, message, source, status,
                    payload_json, created_at
                ) VALUES (
                    :task_id, :event_type, :title, :message, :source, :status,
                    :payload_json, :created_at
                )
                """,
                payload,
            )
            row = conn.execute(
                "SELECT * FROM task_events WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_event(row)

    def _insert_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO task_actions (
                    task_id, action, source, payload_json, result_text, status, created_at
                ) VALUES (
                    :task_id, :action, :source, :payload_json, :result_text,
                    :status, :created_at
                )
                """,
                payload,
            )
            row = conn.execute(
                "SELECT * FROM task_actions WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_action(row)

    def _list_tasks(self, limit: int, status: str, query: str) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if query:
            like = f"%{query}%"
            conditions.append("(title LIKE ? OR request_text LIKE ? OR agent_name LIKE ?)")
            params.extend([like, like, like])
        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM tasks {where_sql} ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [task for row in rows if (task := self._row_to_task(row)) is not None]

    def _get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row_to_task(row)

    def _list_agents(self, unified_msg_origin: str) -> list[dict[str, Any]]:
        """Aggregate distinct agent_id -> latest task summary.

        Scans all tasks (optionally filtered by UMO) and groups by the
        ``agent_id`` stored inside meta_json. Returns one summary per agent
        using its most recently updated task.
        """
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        if unified_msg_origin:
            sql += " WHERE unified_msg_origin = ?"
            params.append(unified_msg_origin)
        sql += " ORDER BY updated_at DESC, created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        agents: dict[str, dict[str, Any]] = {}
        for row in rows:
            task = self._row_to_task(row)
            if task is None:
                continue
            meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
            agent_id = str(meta.get("agent_id") or "")
            if not agent_id or agent_id in agents:
                continue
            agents[agent_id] = {
                "agent_id": agent_id,
                "agent_name": task.get("agent_name", ""),
                "unified_msg_origin": task.get("unified_msg_origin", ""),
                "last_status": task.get("status", ""),
                "last_task_id": task.get("task_id", ""),
                "run_mode": str(meta.get("run_mode") or ""),
                "background_reason": str(meta.get("background_reason") or ""),
                "updated_at": task.get("updated_at", ""),
            }
        return list(agents.values())

    def _get_agent_runs(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all audited tasks whose meta.agent_id matches."""
        if not agent_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        runs: list[dict[str, Any]] = []
        for row in rows:
            task = self._row_to_task(row)
            if task is None:
                continue
            meta = task.get("meta") if isinstance(task.get("meta"), dict) else {}
            if str(meta.get("agent_id") or "") != agent_id:
                continue
            task["run_mode"] = str(meta.get("run_mode") or "")
            task["background_reason"] = str(meta.get("background_reason") or "")
            task["notification_id"] = str(meta.get("notification_id") or "")
            task["agent_id"] = str(meta.get("agent_id") or "")
            runs.append(task)
        return runs

    def _get_task_events(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    def _get_task_actions(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM task_actions WHERE task_id = ? ORDER BY id ASC",
                (task_id,),
            ).fetchall()
        return [self._row_to_action(row) for row in rows]

    def _get_overview(self) -> dict[str, Any]:
        with self._connect() as conn:
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status",
            ).fetchall()
            latest_rows = conn.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 10",
            ).fetchall()
            event_count = conn.execute("SELECT COUNT(*) AS count FROM task_events").fetchone()
        return {
            "statuses": {row["status"]: row["count"] for row in status_rows},
            "latest_tasks": [
                task for row in latest_rows if (task := self._row_to_task(row)) is not None
            ],
            "event_count": int(event_count["count"] if event_count else 0),
            "db_path": str(self.db_path),
        }

    def _export_history(self) -> dict[str, Any]:
        with self._connect() as conn:
            tasks = [self._row_to_task(row) for row in conn.execute("SELECT * FROM tasks")]
            events = [
                self._row_to_event(row)
                for row in conn.execute("SELECT * FROM task_events ORDER BY id ASC")
            ]
            actions = [
                self._row_to_action(row)
                for row in conn.execute("SELECT * FROM task_actions ORDER BY id ASC")
            ]
        return {
            "exported_at": _utcnow(),
            "tasks": [task for task in tasks if task is not None],
            "events": events,
            "actions": actions,
        }

    def _clear_history(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM task_actions")
            conn.execute("DELETE FROM task_events")
            conn.execute("DELETE FROM tasks")
        with self._connect() as conn:
            conn.execute("VACUUM")

    def _delete_task(self, task_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                DELETE FROM task_actions
                WHERE task_id = ?
                   OR task_id IN (SELECT task_id FROM tasks WHERE parent_task_id = ?)
                """,
                (task_id, task_id),
            )
            conn.execute(
                """
                DELETE FROM task_events
                WHERE task_id = ?
                   OR task_id IN (SELECT task_id FROM tasks WHERE parent_task_id = ?)
                """,
                (task_id, task_id),
            )
            conn.execute(
                "DELETE FROM tasks WHERE task_id = ? OR parent_task_id = ?", (task_id, task_id)
            )

    def _update_task_meta(
        self, task_id: str, title: str | None, meta_update: dict[str, Any] | None, now: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            meta = _load_json(data.get("meta_json", "{}"))
            new_title = title if title is not None else data["title"]

            if meta_update:
                meta.update(meta_update)

            meta_json_str = (
                _dump_json(meta) if meta_update is not None else data.get("meta_json", "{}")
            )

            conn.execute(
                "UPDATE tasks SET title = ?, meta_json = ?, updated_at = ? WHERE task_id = ?",
                (new_title, meta_json_str, now, task_id),
            )
            updated_row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            return self._row_to_task(updated_row)
