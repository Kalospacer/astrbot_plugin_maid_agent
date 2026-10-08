"""任务身份、执行轮次及补充投递的持久化记录。"""

from __future__ import annotations

import json
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path

FORMAT_VERSION = 3
ACTIVE_STATUSES = frozenset({"starting", "running", "followup_pending"})


def now_ms() -> int:
    return int(time.time() * 1000)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


class TaskStore:
    """task 不因 session 刷新而失效，每轮结果和投递独立记账。"""

    def __init__(self, root: Path):
        self.root = root / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or len(task_id) != 32 or any(ch not in "0123456789abcdef" for ch in task_id):
            raise ValueError("task_id 格式无效。")
        return self.root / f"{task_id}.json"

    def get(self, task_id: str) -> dict:
        path = self._path(task_id)
        if not path.exists():
            raise ValueError("任务不存在。")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != FORMAT_VERSION:
            raise ValueError("任务格式不受支持。")
        return data

    def save(self, task: dict) -> None:
        task["updatedAt"] = now_ms()
        write_json(self._path(task["taskId"]), task)

    def create(self, *, scope: str, epoch: str, branch: str, request: str) -> dict:
        stamp = now_ms()
        task = {
            "version": FORMAT_VERSION,
            "taskId": uuid.uuid4().hex,
            "scope": scope,
            "epoch": epoch,
            "branchId": branch,
            "request": request,
            "description": " ".join(request.split())[:160],
            "createdAt": stamp,
            "updatedAt": stamp,
            "status": "idle",
            "sessionId": "",
            "rounds": [],
            "followups": [],
        }
        self.save(task)
        return task

    def list(self, *, scope: str, epoch: str) -> list[dict]:
        tasks = []
        for path in self.root.glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == FORMAT_VERSION and data.get("scope") == scope and data.get("epoch") == epoch:
                tasks.append(data)
        return sorted(tasks, key=lambda item: item["createdAt"])

    def start_round(self, task_id: str, session_id: str, request: str) -> dict:
        task = self.get(task_id)
        if task["status"] in ACTIVE_STATUSES:
            raise ValueError("任务正在执行，补充要求请使用 maid_send_message。")
        execution = {
            "roundId": uuid.uuid4().hex,
            "request": request,
            "status": "starting",
            "startedAt": now_ms(),
            "sessionId": session_id,
            "delivery": "pending",
        }
        task.pop("interruptedFromAt", None)
        task["rounds"].append(execution)
        task["status"] = "starting"
        task["sessionId"] = session_id
        self.save(task)
        return deepcopy(execution)

    def mark_running(self, task_id: str, round_id: str) -> None:
        task = self.get(task_id)
        execution = self.round(task, round_id)
        execution["status"] = "running"
        task["status"] = "running"
        self.save(task)

    @staticmethod
    def round(task: dict, round_id: str) -> dict:
        return next(item for item in task["rounds"] if item["roundId"] == round_id)

    def finish(self, task_id: str, round_id: str, result: dict, *, pending: bool = False) -> dict:
        task = self.get(task_id)
        execution = self.round(task, round_id)
        execution.update(deepcopy(result))
        execution["endedAt"] = now_ms()
        task["status"] = "followup_pending" if pending else result["status"]
        self.save(task)
        return deepcopy(execution)

    def add_followup(self, task_id: str, content: str, main_context: list[dict]) -> dict:
        task = self.get(task_id)
        item = {
            "id": uuid.uuid4().hex,
            "content": content,
            "mainContext": deepcopy(main_context),
            "status": "pending",
            "createdAt": now_ms(),
        }
        task["followups"].append(item)
        self.save(task)
        return deepcopy(item)

    def set_followup_status(self, task_id: str, ids: list[str], status: str) -> None:
        task = self.get(task_id)
        for item in task["followups"]:
            if item["id"] in ids:
                item["status"] = status
        self.save(task)

    def pending(self, task_id: str) -> list[dict]:
        return [item for item in self.get(task_id)["followups"] if item["status"] == "pending"]

    def claim_delivery(self, task_id: str, round_id: str) -> bool:
        task = self.get(task_id)
        execution = self.round(task, round_id)
        if execution["delivery"] != "pending":
            return False
        execution["delivery"] = "claimed"
        self.save(task)
        return True

    def delivery(self, task_id: str, round_id: str, status: str) -> None:
        task = self.get(task_id)
        self.round(task, round_id)["delivery"] = status
        self.save(task)

    def interrupt_active(self) -> None:
        """进程恢复只结算中断，不重新执行旧请求或待补充内容。"""
        for path in self.root.glob("*.json"):
            task = self.get(path.stem)
            if task["status"] not in ACTIVE_STATUSES:
                continue
            task["interruptedFromAt"] = task["updatedAt"]
            task["status"] = "interrupted"
            for execution in task["rounds"]:
                if execution["status"] in ACTIVE_STATUSES:
                    execution.update(status="interrupted", error="进程中断，未自动重跑。", endedAt=task["interruptedFromAt"])
            for item in task["followups"]:
                if item["status"] in {"pending", "scheduled"}:
                    item["status"] = "interrupted"
            self.save(task)

    @staticmethod
    def card(task: dict) -> dict:
        return {
            "task_id": task["taskId"],
            "description": task["description"],
            "request": task["request"],
            "status": task["status"],
        }
