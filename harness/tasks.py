"""任务身份、执行轮次及补充投递的持久化记录。"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from pathlib import Path

from .contracts import fingerprint, now_ms, write_json_atomic

FORMAT_VERSION = 3
ACTIVE_STATUSES = frozenset({"starting", "running", "followup_pending"})
ACTIVE_FOLLOWUPS = frozenset({"pending", "scheduled"})


def write_json(path: Path, data: dict) -> None:
    """任务记录保持紧凑，不需要缩进。"""
    write_json_atomic(path, data)


class TaskStore:
    """task 不因 session 刷新而失效，每轮结果和投递独立记账。"""

    def __init__(self, root: Path):
        self.root = root / "tasks"
        self.root.mkdir(parents=True, exist_ok=True)
        self.contexts_dir = self.root / "contexts"
        # 启动时建立轻量索引；工具调用只读取当前作用域内需要的任务。
        self._index: dict[tuple[str, str], dict[str, str]] = {}
        for path in self.root.glob("*.json"):
            task = self.get(path.stem)
            self._index_task(task)

    def _index_task(self, task: dict) -> None:
        self._index.setdefault((task["scope"], task["epoch"]), {})[task["taskId"]] = task["status"]

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
        self._index_task(task)

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
        tasks = [self.get(task_id) for task_id in self._index.get((scope, epoch), {})]
        return sorted(tasks, key=lambda item: item["createdAt"])

    def active(self, *, scope: str, epoch: str) -> list[dict]:
        return [
            self.get(task_id)
            for task_id, status in self._index.get((scope, epoch), {}).items()
            if status in ACTIVE_STATUSES
        ]

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
        context_ref = fingerprint(main_context)
        context_path = self.contexts_dir / f"{context_ref}.json"
        if not context_path.exists():
            write_json(context_path, {"mainContext": main_context})
        item = {
            "id": uuid.uuid4().hex,
            "content": content,
            "mainContextRef": context_ref,
            "status": "pending",
            "createdAt": now_ms(),
        }
        task["followups"].append(item)
        self.save(task)
        return item

    def followup_context(self, item: dict) -> list[dict]:
        # 同一运行格式此前的内联记录仍可读取，后续写入统一使用引用。
        if "mainContext" in item:
            return item["mainContext"]
        path = self.contexts_dir / f"{item['mainContextRef']}.json"
        return json.loads(path.read_text(encoding="utf-8"))["mainContext"]

    def set_followup_status(self, task_id: str, ids: list[str], status: str) -> None:
        task = self.get(task_id)
        for item in task["followups"]:
            if item["id"] in ids:
                item["status"] = status
                if status not in ACTIVE_FOLLOWUPS:
                    item.pop("mainContext", None)
                    item.pop("mainContextRef", None)
        self.save(task)

    def pending(self, task_id: str) -> list[dict]:
        return [item for item in self.get(task_id)["followups"] if item["status"] == "pending"]

    def followup_ids(self, task_id: str) -> list[str]:
        """尚未执行、需要取消或改期的补充要求。"""
        return [
            item["id"]
            for item in self.get(task_id)["followups"]
            if item["status"] in ACTIVE_FOLLOWUPS
        ]

    def cancel_followups(self, task_id: str) -> None:
        self.set_followup_status(task_id, self.followup_ids(task_id), "cancelled")

    def claim_delivery(self, task_id: str, round_id: str) -> bool:
        task = self.get(task_id)
        execution = self.round(task, round_id)
        if execution["delivery"] != "pending":
            return False
        execution["delivery"] = "claimed"
        self.save(task)
        return True

    def release_delivery_claim(self, task_id: str, round_id: str, fallback_status: str = "pending") -> bool:
        """投递取消或异常时安全归还认领。"""
        task = self.get(task_id)
        execution = self.round(task, round_id)
        if execution["delivery"] == "claimed":
            execution["delivery"] = fallback_status
            self.save(task)
            return True
        return False

    def delivery(self, task_id: str, round_id: str, status: str) -> None:
        task = self.get(task_id)
        self.round(task, round_id)["delivery"] = status
        self.save(task)

    def interrupt_active(self) -> None:
        """进程恢复只结算中断，不重新执行旧请求或待补充内容。"""
        for records in self._index.values():
            for task_id in list(records):
                task = self.get(task_id)
                changed = False
                for execution in task["rounds"]:
                    if execution["delivery"] == "claimed":
                        execution["delivery"] = "pending"
                        changed = True
                if task["status"] in ACTIVE_STATUSES:
                    task["interruptedFromAt"] = task["updatedAt"]
                    task["status"] = "interrupted"
                    for execution in task["rounds"]:
                        if execution["status"] in ACTIVE_STATUSES:
                            execution.update(status="interrupted", error="进程中断，未自动重跑。", endedAt=task["interruptedFromAt"])
                    for item in task["followups"]:
                        if item["status"] in ACTIVE_FOLLOWUPS:
                            item["status"] = "interrupted"
                            item.pop("mainContext", None)
                            item.pop("mainContextRef", None)
                    changed = True
                if changed:
                    self.save(task)

    def pending_reports(self):
        """只恢复已结束轮次的报告，不恢复任务执行。"""
        for records in self._index.values():
            for task_id in records:
                task = self.get(task_id)
                for execution in task["rounds"]:
                    if execution["status"] not in ACTIVE_STATUSES and execution["delivery"] == "pending":
                        yield task, execution

    def retention_prune(self, retention_days: int, live_epochs: set[str]) -> None:
        """清理过期的失效代次；当前主对话的 task 身份始终保留。"""
        cutoff = now_ms() - max(1, retention_days) * 86_400_000
        referenced = set()
        for key, records in list(self._index.items()):
            for task_id in list(records):
                task = self.get(task_id)
                if task["epoch"] not in live_epochs and task["status"] not in ACTIVE_STATUSES and task["updatedAt"] < cutoff:
                    self._path(task_id).unlink()
                    del records[task_id]
                    continue
                referenced.update(item["mainContextRef"] for item in task["followups"] if "mainContextRef" in item)
            if not records:
                del self._index[key]
        for path in self.contexts_dir.glob("*.json"):
            if path.stem not in referenced:
                path.unlink()

    @staticmethod
    def card(task: dict) -> dict:
        return {
            "task_id": task["taskId"],
            "description": task["description"],
            "request": task["request"],
            "status": task["status"],
        }
