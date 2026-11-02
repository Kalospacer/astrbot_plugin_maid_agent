"""主对话级路由、执行代次和空闲窗口。"""

from __future__ import annotations

import asyncio
import hashlib
import uuid

from .contracts import now_ms
from .db import Database, dump, load
from .tasks import TaskStore, stage


class ChatStateStore:
    def __init__(self, db: Database, tasks: TaskStore, clock=now_ms):
        self.db = db
        self.tasks = tasks
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, umo: str) -> asyncio.Lock:
        return self._locks.setdefault(umo, asyncio.Lock())

    @staticmethod
    def key(umo: str) -> str:
        return hashlib.sha256(umo.encode()).hexdigest()

    def get(self, umo: str) -> dict | None:
        row = self.db.execute("SELECT value FROM chat_states WHERE umo = ?", (umo,)).fetchone()
        return load(row[0]) if row else None

    def _all(self) -> list[dict]:
        return [load(row[0]) for row in self.db.execute("SELECT value FROM chat_states").fetchall()]

    async def unbind_session(self, session_id: str, before_remove=None) -> None:
        """与派发共用聊天锁，锁内重新读取绑定并检查是否可删除。"""
        for previous in self._all():
            if session_id not in previous["branches"].values():
                continue
            async with self.lock(previous["umo"]):
                state = self.get(previous["umo"])
                if state is None:
                    continue
                if before_remove is not None:
                    before_remove()
                if session_id in state["branches"].values():
                    state["branches"] = {b: sid for b, sid in state["branches"].items() if sid != session_id}
                    if state["defaultBranch"] not in state["branches"]:
                        state["defaultBranch"] = None
                    self.save(state)

    def save(self, state: dict) -> None:
        self.db.execute(
            "INSERT INTO chat_states (umo, value) VALUES (?, ?) ON CONFLICT (umo) DO UPDATE SET value = excluded.value",
            (state["umo"], dump(state)),
        )

    def reset(self, umo: str, conversation_id: str) -> dict:
        state = {
            "umo": umo,
            "conversationId": conversation_id,
            "epoch": uuid.uuid4().hex,
            "contextGeneration": 0,
            "branches": {},
            "defaultBranch": None,
            "idleSince": self.clock(),
            "lastToolAt": self.clock(),
        }
        self.save(state)
        return state

    def ensure(self, umo: str, conversation_id: str) -> dict:
        state = self.get(umo)
        if state is None or state["conversationId"] != conversation_id:
            return self.reset(umo, conversation_id)
        return state

    def task_list(self, state: dict) -> list[dict]:
        return self.tasks.list(scope=self.key(state["umo"]), epoch=state["epoch"])

    def working(self, state: dict) -> list[dict]:
        """当前代次里管家正在处理或排队的任务。"""
        return [task for task in self.task_list(state) if stage(task).startswith("working")]

    def open(self, state: dict) -> list[dict]:
        """当前代次里尚未结束的任务。"""
        return [task for task in self.task_list(state) if not task.get("ended")]

    async def retention_prune(self, retention_days: int) -> set[str]:
        cutoff = self.clock() - max(1, retention_days) * 86_400_000
        live_epochs: set[str] = set()
        for previous in self._all():
            async with self.lock(previous["umo"]):
                state = self.get(previous["umo"])
                if state is None:
                    continue
                if not state["branches"] and state["lastToolAt"] < cutoff and not self.working(state):
                    self.db.execute("DELETE FROM chat_states WHERE umo = ?", (state["umo"],))
                else:
                    live_epochs.add(state["epoch"])
        return live_epochs

    def touch_tool(self, state: dict, timeout_hours: float) -> bool:
        """先失效过期 session，再刷新活跃时间；task 身份不受影响。"""
        now = self.clock()
        working = bool(self.working(state))
        idle_since = state["idleSince"]
        expired = not working and idle_since is not None and now - idle_since >= timeout_hours * 3_600_000
        if expired:
            state["contextGeneration"] += 1
            state["branches"] = {}
            state["defaultBranch"] = None
        state["lastToolAt"] = now
        state["idleSince"] = None if working else now
        self.save(state)
        return expired

    def begin(self, state: dict) -> None:
        state["idleSince"] = None
        self.save(state)

    def settle(self, state: dict) -> None:
        if not self.working(state):
            state["idleSince"] = self.clock()
            self.save(state)

    def recover(self) -> None:
        """启动时补上空闲计时：用最后一次工具调用与任务更新的较晚者。"""
        for state in self._all():
            if state["idleSince"] is None:
                stamps = [state["lastToolAt"], *(task["updatedAt"] for task in self.task_list(state))]
                state["idleSince"] = max(stamps)
                self.save(state)

    def target(self, state: dict, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task["scope"] != self.key(state["umo"]) or task["epoch"] != state["epoch"]:
            raise ValueError("任务不属于当前主对话。")
        return task

    @staticmethod
    def automatic_branch(state: dict) -> str | None:
        if state["defaultBranch"] in state["branches"]:
            return state["defaultBranch"]
        if len(state["branches"]) == 1:
            return next(iter(state["branches"]))
        return None
