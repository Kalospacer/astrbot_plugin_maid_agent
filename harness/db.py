"""插件唯一的 SQLite 数据库：建表、事务、提交后回调，以及持久化副作用队列。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._log import logger
from .contracts import now_ms

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, header TEXT NOT NULL, meta TEXT NOT NULL);
CREATE TABLE session_events (
    session_id TEXT NOT NULL, seq INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY (session_id, seq)
);
CREATE TABLE contexts (session_id TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE context_checkpoints (
    session_id TEXT NOT NULL, turn INTEGER NOT NULL, value TEXT NOT NULL, PRIMARY KEY (session_id, turn)
);
CREATE TABLE chat_states (umo TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, epoch TEXT NOT NULL, created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL, open INTEGER NOT NULL, value TEXT NOT NULL
);
CREATE INDEX tasks_scope ON tasks (scope, epoch);
CREATE TABLE decisions (
    id TEXT PRIMARY KEY, at INTEGER NOT NULL, kind TEXT NOT NULL, task_id TEXT NOT NULL, value TEXT NOT NULL
);
CREATE INDEX decisions_at ON decisions (at);
CREATE INDEX decisions_task ON decisions (task_id);
CREATE TABLE effects (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, due_at INTEGER NOT NULL,
    UNIQUE (kind, key)
);
CREATE TABLE receipts (id TEXT PRIMARY KEY, at INTEGER NOT NULL);
"""


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def load(text: str) -> Any:
    return json.loads(text)


def _lock_exclusively(handle) -> None:
    """同一份数据只允许一个进程写；拿不到锁说明另一个实例正在使用。"""
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


class Database:
    """单进程独占的 SQLite 连接；多条写入经由 transaction() 一起提交。

    事务块内不能 await：所有协程共用这一个连接，跨 await 会把别人的写入卷进来。
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # 锁文件在实例存活期间一直打开，close() 时才释放。
        self._lock_handle = open(path.with_suffix(".lock"), "a+b")  # noqa: SIM115
        try:
            _lock_exclusively(self._lock_handle)
        except OSError as exc:
            self._lock_handle.close()
            raise RuntimeError(f"数据库正被另一个进程使用: {path}") from exc

        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        os.chmod(path, 0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self._depth = 0
        self._after_commit: list[Callable[[], None]] = []

        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone() is not None
        if version == 0 and not has_tables:
            self.connection.executescript(f"BEGIN; {_SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
            logger.info("[maid] 数据库已创建: %s (schema %d)", path, SCHEMA_VERSION)
        elif version != SCHEMA_VERSION:
            self.close()
            raise RuntimeError(f"数据库 schema 版本 {version} 不受支持（需要 {SCHEMA_VERSION}）: {path}")

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """执行一条语句；不在事务里时立即提交。"""
        return self.connection.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """嵌套调用并入最外层事务；最外层提交后依次执行登记的回调。"""
        if self._depth == 0:
            self.connection.execute("BEGIN IMMEDIATE")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self.connection.execute("ROLLBACK")
                self._after_commit.clear()
            raise
        self._depth -= 1
        if self._depth > 0:
            return

        self.connection.execute("COMMIT")
        callbacks, self._after_commit = self._after_commit, []
        for callback in callbacks:
            callback()

    def after_commit(self, callback: Callable[[], None]) -> None:
        """事务提交后执行；不在事务里时立即执行。"""
        if self._depth == 0:
            callback()
        else:
            self._after_commit.append(callback)

    def close(self) -> None:
        self.connection.close()
        self._lock_handle.close()


class Later(Exception):
    """副作用暂时不能执行：按初始间隔再试，不计失败次数。"""


@dataclass(frozen=True, slots=True)
class EffectHandler:
    run: Callable[[dict, str], Awaitable[None]]
    first_delay_ms: int
    max_delay_ms: int
    ordered: bool


@dataclass(frozen=True, slots=True)
class _EffectRow:
    seq: int
    kind: str
    key: str
    payload: dict
    generation: int
    attempts: int


class EffectQueue:
    """持久化副作用：在调用方事务里登记，提交后执行，失败按指数退避重试。

    同一 (kind, key) 只保留一行，重复登记会替换内容并保留排队位置；有序种类
    一次只跑最早的一行，后面的行等它完成。
    """

    def __init__(self, db: Database):
        self.db = db
        self.handlers: dict[str, EffectHandler] = {}
        self._wakeup = asyncio.Event()
        self._dispatcher: asyncio.Task | None = None
        self._running: dict[int, asyncio.Task] = {}

    def handle(self, kind: str, run: Callable[[dict, str], Awaitable[None]], *, retry: tuple[int, int], ordered: bool = False) -> None:
        self.handlers[kind] = EffectHandler(run, retry[0], retry[1], ordered)

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        self.db.execute(
            "INSERT INTO effects (kind, key, payload, due_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (kind, key) DO UPDATE SET payload = excluded.payload, "
            "generation = generation + 1, attempts = 0, due_at = excluded.due_at",
            (kind, key, dump(payload), now_ms()),
        )
        self.db.after_commit(self._wakeup.set)

    def payloads(self, kind: str) -> list[dict]:
        rows = self.db.execute("SELECT payload FROM effects WHERE kind = ? ORDER BY seq", (kind,)).fetchall()
        return [load(row[0]) for row in rows]

    def start(self) -> None:
        self._dispatcher = asyncio.create_task(self._dispatch_forever(), name="maid-effects")

    async def stop(self) -> None:
        """停止派发并取消正在执行的副作用；未完成的行留到下次启动。"""
        tasks = [task for task in (self._dispatcher, *self._running.values()) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    async def _dispatch_forever(self) -> None:
        while True:
            self._wakeup.clear()
            delay = self._start_due()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wakeup.wait(), timeout=delay)

    def _start_due(self) -> float | None:
        """启动所有到期且可运行的行，返回距最近一行到期的秒数。"""
        now = now_ms()
        next_due: int | None = None
        held_kinds: set[str] = set()
        rows = self.db.execute(
            "SELECT seq, kind, key, payload, generation, attempts, due_at FROM effects ORDER BY seq"
        ).fetchall()

        for seq, kind, key, payload, generation, attempts, due_at in rows:
            handler = self.handlers.get(kind)
            if handler is None or kind in held_kinds:
                continue
            if handler.ordered:
                held_kinds.add(kind)
            if seq in self._running:
                continue
            if due_at > now:
                next_due = due_at if next_due is None else min(next_due, due_at)
                continue
            row = _EffectRow(seq, kind, key, load(payload), generation, attempts)
            self._running[seq] = asyncio.create_task(self._run(handler, row), name=f"maid-effect-{kind}")

        return None if next_due is None else max(0.05, (next_due - now) / 1000)

    async def _run(self, handler: EffectHandler, row: _EffectRow) -> None:
        try:
            await handler.run(row.payload, row.key)
        except asyncio.CancelledError:
            raise
        except Later:
            self._reschedule(row, row.attempts, handler.first_delay_ms)
        except Exception as exc:  # noqa: BLE001
            delay = min(handler.first_delay_ms * 2**row.attempts, handler.max_delay_ms)
            logger.warning("[maid] 副作用 %s:%s 第 %d 次失败，%ds 后重试: %s", row.kind, row.key[:16], row.attempts + 1, delay // 1000, exc)
            self._reschedule(row, row.attempts + 1, delay)
        else:
            self.db.execute("DELETE FROM effects WHERE seq = ? AND generation = ?", (row.seq, row.generation))
        finally:
            self._running.pop(row.seq, None)
            self._wakeup.set()

    def _reschedule(self, row: _EffectRow, attempts: int, delay_ms: int) -> None:
        # 运行期间被重新登记过（generation 变了）的行保持新内容立即到期。
        self.db.execute(
            "UPDATE effects SET attempts = ?, due_at = ? WHERE seq = ? AND generation = ?",
            (attempts, now_ms() + delay_ms, row.seq, row.generation),
        )
