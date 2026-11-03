"""任务、执行、输入与上报的持久化，以及由此推导的任务阶段。

一个任务由若干次执行（assignment）推进。一次执行绑定一个管家会话，贯穿管家的多轮，
直到任务结束或宿主重启。输入（input）是送给管家的文字；上报（report）是管家或宿主
对当前执行状态的说明。任务文档整份存在 tasks 表里。
"""

from __future__ import annotations

import uuid

from .contracts import now_ms
from .db import Database, dump, load

REPORT_STATES = ("progress", "waiting", "ready", "blocked", "failed", "dropped")
MAID_REPORT_STATES = tuple(state for state in REPORT_STATES if state != "waiting")
SETTLED_REPORT_STATES = frozenset({"waiting", "ready", "blocked", "failed"})
HUMAN_ACTION_MAX_CHARS = 1000
CARD_SUMMARY_CHARS = 300


def _valid_task_id(task_id: object) -> bool:
    return isinstance(task_id, str) and len(task_id) == 32 and all(ch in "0123456789abcdef" for ch in task_id)


def latest_assignment(task: dict) -> dict | None:
    return task["assignments"][-1] if task["assignments"] else None


def running_assignment(task: dict) -> dict | None:
    return next((item for item in task["assignments"] if item["state"] == "running"), None)


def latest_report(task: dict) -> dict | None:
    """任务最近说过的话：最后一次带上报的执行的上报。"""
    for assignment in reversed(task["assignments"]):
        if assignment.get("report"):
            return assignment["report"]
    return None


def delivered_since_report(assignment: dict) -> bool:
    report = assignment.get("report")
    return bool(report and assignment.get("deliveredAt") and assignment["deliveredAt"] >= report["at"])


def waits_for_person(task: dict) -> bool:
    """最近一次上报是 blocked/failed，且之后没有新输入。"""
    assignment = latest_assignment(task)
    report = (assignment or {}).get("report")
    if report is None or report["state"] not in ("blocked", "failed"):
        return False
    return not any(item["kind"] == "text" and item["at"] > report["at"] for item in task["inputs"])


def stage(task: dict) -> str:
    """任务阶段，按顺序取第一条命中的规则（对应 superagent core/stage.ts）。"""
    if task.get("ended"):
        return "ended"
    if task.get("delivery"):
        return "person/delivery"
    if task.get("attention"):
        return "person/answer"
    assignment = latest_assignment(task)
    report = (assignment or {}).get("report") or {}
    if waits_for_person(task):
        return f"person/{report['state']}"
    if assignment is None or assignment["state"] == "pending":
        return "working/queued"
    if report.get("state") == "waiting" or assignment["state"] == "ended":
        return "idle"
    return "working"


def new_assignment(task: dict, session_id: str) -> dict:
    """追加一次待启动的执行；调用方负责保存并登记 launch。"""
    assignment = {
        "id": uuid.uuid4().hex,
        "state": "pending",
        "sessionId": session_id,
        "createdAt": now_ms(),
        "report": None,
    }
    task["assignments"].append(assignment)
    task["sessionId"] = session_id
    return assignment


def add_input(
    task: dict,
    text: str,
    *,
    human: bool,
    kind: str = "text",
    full: str = "",
    trigger: str = "",
    images: list | None = None,
) -> dict:
    """追加一条输入。text 是送给管家的文字；full 和 trigger 是请求原文与触发消息原文，供 maid_read 读取。"""
    item = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "text": text,
        "human": human,
        "at": now_ms(),
        "deliveredTo": None,
        "readAt": None,
    }
    if full:
        item["full"] = full
    if trigger:
        item["trigger"] = trigger
    if images:
        item["images"] = images
    task["inputs"].append(item)
    if kind == "text":
        task["undelivered"].append(item["id"])
    return item


def take_undelivered(task: dict, assignment: dict, *, read: bool) -> list[dict]:
    """把待送达的输入记到这次执行名下并返回；read 表示它们作为新一轮的首条消息，立即被读到。"""
    at = now_ms()
    taken = [item for item in task["inputs"] if item["id"] in task["undelivered"]]
    for item in taken:
        item["deliveredTo"] = assignment["id"]
        item["readAt"] = at if read else None
    task["undelivered"] = []
    task["delivery"] = None
    if taken:
        assignment["deliveredAt"] = at
    return taken


def requeue_unread(task: dict, assignment: dict, input_ids: set[str] | None = None) -> None:
    """送到这次执行却没被读到的输入放回待送达队首；input_ids 为空时取全部未读。"""
    unread = [
        item["id"]
        for item in task["inputs"]
        if item["deliveredTo"] == assignment["id"]
        and item["readAt"] is None
        and (input_ids is None or item["id"] in input_ids)
        and item["id"] not in task["undelivered"]
    ]
    task["undelivered"] = [*unread, *task["undelivered"]]


def end_assignment(task: dict, assignment: dict, reason: str) -> None:
    if assignment["state"] == "ended":
        return
    assignment["state"] = "ended"
    assignment["endedAt"] = now_ms()
    assignment["endedReason"] = reason


def end_task(task: dict, *, by: str, outcome: str, reason: str = "") -> None:
    """结束任务：结束当前执行，放弃没送达的输入和未答的问题。"""
    for assignment in task["assignments"]:
        end_assignment(task, assignment, outcome)
    task["ended"] = {"at": now_ms(), "by": by, "outcome": outcome, **({"reason": reason} if reason else {})}
    task["undelivered"] = []
    task["attention"] = None
    task["delivery"] = None


def record_report(task: dict, *, state: str, summary: str, question: str | None = None, keep_question: bool = True) -> dict:
    """在当前执行上记一次上报并更新未答问题；问题不能和 ready 一起给。

    keep_question=True 且没给 question 时保留原问题；question=None 且 keep_question=False 时清除。
    """
    if task.get("ended"):
        raise ValueError("任务已结束，不要继续这项工作。")
    if state not in REPORT_STATES:
        raise ValueError(f"state 必须是 {', '.join(REPORT_STATES)} 之一。")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("summary 必须是非空字符串。")
    if question is not None:
        if not isinstance(question, str) or not question.strip() or len(question.strip()) > HUMAN_ACTION_MAX_CHARS:
            raise ValueError(f"human_action 必须是 1 至 {HUMAN_ACTION_MAX_CHARS} 字的问题。")
        if state == "ready":
            raise ValueError("human_action 不能和 ready 一起上报。")
    assignment = running_assignment(task) or latest_assignment(task)
    if assignment is None or assignment["state"] == "ended":
        raise ValueError("任务当前没有进行中的执行。")

    at = now_ms()
    previous = task.get("attention")
    if question is not None and (previous is None or previous["question"] != question.strip()):
        task["attention"] = {"id": uuid.uuid4().hex, "question": question.strip(), "at": at}
    elif state == "ready" or (question is None and not keep_question):
        task["attention"] = None

    report = {"id": uuid.uuid4().hex, "state": state, "summary": summary.strip(), "at": at}
    if question is not None:
        report["question"] = question.strip()
    assignment["report"] = report
    return report


class TaskStore:
    """任务文档的读写；修改任务用本模块的函数，再交给 save。"""

    def __init__(self, db: Database):
        self.db = db

    def get(self, task_id: str) -> dict:
        if not _valid_task_id(task_id):
            raise ValueError("task_id 格式无效。")
        row = self.db.execute("SELECT value FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("任务不存在。")
        return load(row[0])

    def save(self, task: dict) -> None:
        task["updatedAt"] = now_ms()
        self.db.execute(
            "INSERT INTO tasks (id, scope, epoch, created_at, updated_at, open, value) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET updated_at = excluded.updated_at, open = excluded.open, value = excluded.value",
            (
                task["taskId"],
                task["scope"],
                task["epoch"],
                task["createdAt"],
                task["updatedAt"],
                0 if task.get("ended") else 1,
                dump(task),
            ),
        )

    def create(
        self,
        *,
        scope: str,
        epoch: str,
        umo: str,
        conversation_id: str,
        branch: str,
        request: str,
        strength: str,
    ) -> dict:
        """新建任务；strength 是 explicit / inferred / console。"""
        stamp = now_ms()
        task = {
            "taskId": uuid.uuid4().hex,
            "scope": scope,
            "epoch": epoch,
            "umo": umo,
            "conversationId": conversation_id,
            "branchId": branch,
            "sessionId": "",
            "description": " ".join(request.split())[:160],
            "strength": strength,
            "createdAt": stamp,
            "updatedAt": stamp,
            "assignments": [],
            "inputs": [],
            "undelivered": [],
            "attention": None,
            "delivery": None,
            "ended": None,
        }
        return task

    def list(self, *, scope: str, epoch: str, open_only: bool = False) -> list[dict]:
        sql = "SELECT value FROM tasks WHERE scope = ? AND epoch = ?"
        if open_only:
            sql += " AND open = 1"
        rows = self.db.execute(sql + " ORDER BY created_at", (scope, epoch)).fetchall()
        return [load(row[0]) for row in rows]

    def latest_update(self, *, scope: str, epoch: str) -> int | None:
        return self.db.execute(
            "SELECT MAX(updated_at) FROM tasks WHERE scope = ? AND epoch = ?", (scope, epoch)
        ).fetchone()[0]

    def ids_for_session(self, session_id: str) -> list[str]:
        """当前绑定在这个管家会话上的任务。"""
        rows = self.db.execute("SELECT id FROM tasks WHERE json_extract(value, '$.sessionId') = ?", (session_id,)).fetchall()
        return [row[0] for row in rows]

    def open_tasks(self) -> list[dict]:
        rows = self.db.execute("SELECT value FROM tasks WHERE open = 1 ORDER BY created_at").fetchall()
        return [load(row[0]) for row in rows]

    def retention_prune(self, retention_days: int, live_epochs: set[str]) -> None:
        """清理过期的失效代次里已结束的任务；当前主对话的任务始终保留。"""
        cutoff = now_ms() - max(1, retention_days) * 86_400_000
        rows = self.db.execute("SELECT id, epoch FROM tasks WHERE open = 0 AND updated_at < ?", (cutoff,)).fetchall()
        with self.db.transaction():
            for task_id, epoch in rows:
                if epoch not in live_epochs:
                    self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    @staticmethod
    def card(task: dict) -> dict:
        card = {"task_id": task["taskId"], "description": task["description"], "stage": stage(task)}
        report = latest_report(task)
        if report is not None:
            summary = report["summary"]
            card["report"] = {
                "state": report["state"],
                "summary": summary if len(summary) <= CARD_SUMMARY_CHARS else summary[:CARD_SUMMARY_CHARS] + "…",
            }
        if task.get("attention"):
            card["question"] = task["attention"]["question"]
        if task.get("ended"):
            card["ended"] = {"by": task["ended"]["by"], "outcome": task["ended"]["outcome"]}
        return card
