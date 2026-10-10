"""主对话级路由、执行代次和空闲窗口。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path

from .tasks import FORMAT_VERSION, TaskStore, now_ms, write_json


class ChatStateStore:
    def __init__(self, root: Path, tasks: TaskStore, clock=now_ms):
        self.root = root / "chat_states"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tasks = tasks
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, umo: str) -> asyncio.Lock:
        return self._locks.setdefault(umo, asyncio.Lock())

    @staticmethod
    def key(umo: str) -> str:
        return hashlib.sha256(umo.encode()).hexdigest()

    def _path(self, umo: str) -> Path:
        return self.root / f"{self.key(umo)}.json"

    @staticmethod
    def _read(path: Path) -> dict:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != FORMAT_VERSION:
            raise ValueError("主对话状态格式不受支持。")
        return data

    def get(self, umo: str) -> dict | None:
        path = self._path(umo)
        if not path.exists():
            return None
        return self._read(path)

    async def unbind_session(self, session_id: str, before_remove=None) -> None:
        """与派发共用聊天锁，锁内重新读取绑定并检查是否可删除。"""
        for path in self.root.glob("*.json"):
            previous = self._read(path)
            if session_id not in previous["branches"].values():
                continue
            async with self.lock(previous["umo"]):
                state = self.get(previous["umo"])
                if state is None:
                    continue
                if before_remove is not None:
                    before_remove()
                removed = [branch for branch, sid in state["branches"].items() if sid == session_id]
                for branch in removed:
                    del state["branches"][branch]
                if state["defaultBranch"] in removed:
                    state["defaultBranch"] = None
                self.save(state)

    def save(self, state: dict) -> None:
        write_json(self._path(state["umo"]), state)

    def reset(self, umo: str, conversation_id: str) -> dict:
        state = {
            "version": FORMAT_VERSION,
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

    def active(self, state: dict) -> list[dict]:
        return self.tasks.active(scope=self.key(state["umo"]), epoch=state["epoch"])

    async def retention_prune(self, retention_days: int) -> None:
        cutoff = self.clock() - max(1, retention_days) * 86_400_000
        for path in self.root.glob("*.json"):
            previous = self._read(path)
            async with self.lock(previous["umo"]):
                state = self.get(previous["umo"])
                if state is not None and not state["branches"] and state["lastToolAt"] < cutoff and not self.task_list(state):
                    path.unlink()

    def touch_tool(self, state: dict, timeout_hours: float) -> bool:
        """先失效过期 session，再刷新活跃时间；task 身份不受影响。"""
        now = self.clock()
        active = self.active(state)
        idle_since = state["idleSince"]
        expired = not active and idle_since is not None and now - idle_since >= timeout_hours * 3_600_000
        if expired:
            state["contextGeneration"] += 1
            state["branches"] = {}
            state["defaultBranch"] = None
        state["lastToolAt"] = now
        state["idleSince"] = None if active else now
        self.save(state)
        return expired

    def begin(self, state: dict) -> None:
        state["idleSince"] = None
        self.save(state)

    def settle(self, state: dict) -> None:
        if not self.active(state):
            state["idleSince"] = self.clock()
            self.save(state)

    def recover(self) -> None:
        """保留已有空闲计时；恢复中断执行时使用最后持久化活动时间。"""
        for path in self.root.glob("*.json"):
            state = self._read(path)
            if state["idleSince"] is None:
                stamps = [state["lastToolAt"]]
                stamps.extend(task.get("interruptedFromAt", task["updatedAt"]) for task in self.task_list(state))
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
