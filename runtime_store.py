"""
大小姐管家模式插件 - 1.3.0 Runtime 持久化层

与旧 sessions/*.json 完全隔离的新存储：

    <data_dir>/agents/<agent_id>/agent.json          # agent 身份与活跃 run
    <data_dir>/agents/<agent_id>/transcript.jsonl    # append-only 对话轨迹
    <data_dir>/agents/<agent_id>/runs/<task_id>.json # 单次 run 元数据 + 通知
    <data_dir>/agents/<agent_id>/outputs/<task_id>.txt # 结果输出

设计原则（对齐 Claude Code subagent runtime）：
- agent_id 跨 resume 稳定；task_id 每次执行独立。
- transcript 仅追加，resume 时重建，过滤损坏尾部与未配对 tool calls。
- run 终态与 pending notification 写入同一原子替换的 metadata，保证崩溃后结果可发现。
- 30 天无活动清理 transcript/agent，不清理 memory 与旧 sessions。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.api.star import StarTools

from .constants import PLUGIN_DATA_DIR_NAME

if TYPE_CHECKING:
    from .config import MaidModeConfig

UTC = timezone.utc

AGENTS_SUBDIR = "agents"
TRANSCRIPT_FILENAME = "transcript.jsonl"
AGENT_META_FILENAME = "agent.json"
RUNS_SUBDIR = "runs"
OUTPUTS_SUBDIR = "outputs"

# JSONL control record kinds.
CTRL_RUN_START = "run_start"
CTRL_RUN_END = "run_end"
CTRL_STEER = "steer"
CTRL_STOP = "stop"
CTRL_RESUME = "resume"
CTRL_TOOL_START = "tool_start"
CTRL_TOOL_END = "tool_end"

# Terminal run statuses (mirrors orchestrator state machine).
TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})
# Active-ish statuses that on restart must collapse to "interrupted".
ACTIVE_RUN_STATUSES = frozenset({"starting", "running"})

_AGENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TASK_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utcnow().isoformat()


def _new_agent_id() -> str:
    return uuid.uuid4().hex


def _new_task_id() -> str:
    return uuid.uuid4().hex


def _validate_hex_id(value: str, label: str) -> str:
    normalized = (value or "").strip().casefold()
    if not _AGENT_ID_RE.fullmatch(normalized):
        raise ValueError(f"非法 {label}: {value!r}")
    return normalized


def _safe_path(base: Path, child: str) -> Path:
    """Resolve child under base, refusing traversal escapes."""
    resolved = (base / child).resolve()
    base_resolved = base.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"路径越界: {child!r}") from exc
    return resolved


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[大小姐模式] 读取 JSON 失败，已跳过: path=%s err=%s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, tolerating a corrupted trailing line by truncating it."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    raw_lines: list[str]
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.warning("[大小姐模式] 读取 transcript 失败: path=%s err=%s", path, exc)
        return records

    for index, line in enumerate(raw_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            # Corrupt tail: drop it and everything after (append-only invariant
            # means anything past the first bad line is unreliable).
            if records:
                logger.warning(
                    "[大小姐模式] transcript 第 %d 行损坏，已截断后续记录: path=%s",
                    index + 1,
                    path,
                )
            break
        if isinstance(record, dict):
            records.append(record)
    return records


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


@dataclass(slots=True)
class PendingNotification:
    """A terminal run result awaiting opportunistic delivery to the main agent."""

    notification_id: str
    agent_id: str
    task_id: str
    unified_msg_origin: str
    status: str
    result: str = ""
    error: str = ""
    created_at: str = field(default_factory=_iso_now)
    delivered: bool = False
    delivered_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PendingNotification:
        return cls(
            notification_id=str(data.get("notification_id") or _new_agent_id()),
            agent_id=str(data.get("agent_id") or ""),
            task_id=str(data.get("task_id") or ""),
            unified_msg_origin=str(data.get("unified_msg_origin") or ""),
            status=str(data.get("status") or "failed"),
            result=str(data.get("result") or ""),
            error=str(data.get("error") or ""),
            created_at=str(data.get("created_at") or _iso_now()),
            delivered=bool(data.get("delivered", False)),
            delivered_at=str(data.get("delivered_at") or ""),
        )


@dataclass(slots=True)
class RunMeta:
    """Per-execution metadata. Terminal status and pending notification are
    written in the same atomic replacement so the result is discoverable after a
    crash."""

    task_id: str
    agent_id: str
    unified_msg_origin: str
    agent_name: str
    sender_id: str
    mode: str  # "foreground" | "background"
    status: str  # starting|running|completed|failed|stopped|interrupted
    request_text: str = ""
    resume_of: str = ""  # agent_id resumed from, if any
    background_reason: str = ""  # "timeout"|"explicit"|"resume"|...
    created_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    started_at: str = ""
    ended_at: str = ""
    result: str = ""
    error: str = ""
    output_file: str = ""
    notification: PendingNotification | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["notification"] = (
            self.notification.to_dict() if self.notification is not None else None
        )
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMeta:
        notification_raw = data.get("notification")
        notification = (
            PendingNotification.from_dict(notification_raw)
            if isinstance(notification_raw, dict)
            else None
        )
        return cls(
            task_id=str(data.get("task_id") or ""),
            agent_id=str(data.get("agent_id") or ""),
            unified_msg_origin=str(data.get("unified_msg_origin") or ""),
            agent_name=str(data.get("agent_name") or ""),
            sender_id=str(data.get("sender_id") or ""),
            mode=str(data.get("mode") or "background"),
            status=str(data.get("status") or "starting"),
            request_text=str(data.get("request_text") or ""),
            resume_of=str(data.get("resume_of") or ""),
            background_reason=str(data.get("background_reason") or ""),
            created_at=str(data.get("created_at") or _iso_now()),
            updated_at=str(data.get("updated_at") or _iso_now()),
            started_at=str(data.get("started_at") or ""),
            ended_at=str(data.get("ended_at") or ""),
            result=str(data.get("result") or ""),
            error=str(data.get("error") or ""),
            output_file=str(data.get("output_file") or ""),
            notification=notification,
        )


@dataclass(slots=True)
class AgentMeta:
    """Stable agent identity. Carries the single active task_id (at most one
    active run per agent) and the last activity timestamp for retention."""

    agent_id: str
    unified_msg_origin: str
    agent_name: str
    sender_id: str
    created_at: str = field(default_factory=_iso_now)
    updated_at: str = field(default_factory=_iso_now)
    active_task_id: str = ""
    last_status: str = "starting"
    last_task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentMeta:
        return cls(
            agent_id=str(data.get("agent_id") or ""),
            unified_msg_origin=str(data.get("unified_msg_origin") or ""),
            agent_name=str(data.get("agent_name") or ""),
            sender_id=str(data.get("sender_id") or ""),
            created_at=str(data.get("created_at") or _iso_now()),
            updated_at=str(data.get("updated_at") or _iso_now()),
            active_task_id=str(data.get("active_task_id") or ""),
            last_status=str(data.get("last_status") or "starting"),
            last_task_id=str(data.get("last_task_id") or ""),
        )


class RuntimeStore:
    """Filesystem-backed runtime store for 1.3.0 subagent runs.

    All writes are atomic (temp file + os.replace) or append-only (transcript).
    A single asyncio lock guards the whole store for simplicity; runtime state
    mutations are low-frequency compared to transcript appends.
    """

    def __init__(self, config: MaidModeConfig) -> None:
        self.config = config
        self.data_dir = StarTools.get_data_dir(PLUGIN_DATA_DIR_NAME)
        self.agents_dir = self.data_dir / AGENTS_SUBDIR
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    @staticmethod
    def iso_now() -> str:
        return _iso_now()

    # ------------------------------------------------------------------ paths

    def _agent_dir(self, agent_id: str) -> Path:
        aid = _validate_hex_id(agent_id, "agent_id")
        return _safe_path(self.agents_dir, aid)

    def _agent_meta_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / AGENT_META_FILENAME

    def _transcript_path(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / TRANSCRIPT_FILENAME

    def _runs_dir(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / RUNS_SUBDIR

    def _outputs_dir(self, agent_id: str) -> Path:
        return self._agent_dir(agent_id) / OUTPUTS_SUBDIR

    def _run_meta_path(self, agent_id: str, task_id: str) -> Path:
        tid = _validate_hex_id(task_id, "task_id")
        return _safe_path(self._runs_dir(agent_id), f"{tid}.json")

    def _output_path(self, agent_id: str, task_id: str) -> Path:
        tid = _validate_hex_id(task_id, "task_id")
        return _safe_path(self._outputs_dir(agent_id), f"{tid}.txt")

    # ------------------------------------------------------------ agent meta

    async def create_agent(
        self,
        *,
        unified_msg_origin: str,
        agent_name: str,
        sender_id: str,
    ) -> AgentMeta:
        agent_id = _new_agent_id()
        meta = AgentMeta(
            agent_id=agent_id,
            unified_msg_origin=unified_msg_origin,
            agent_name=agent_name,
            sender_id=sender_id,
        )
        async with self._lock:
            self._agent_dir(agent_id).mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
        logger.info(
            "[大小姐模式] 已创建 runtime agent: agent_id=%s agent=%s umo=%s",
            agent_id,
            agent_name,
            unified_msg_origin,
        )
        return meta

    async def load_agent(self, agent_id: str) -> AgentMeta | None:
        async with self._lock:
            data = _read_json(self._agent_meta_path(agent_id))
        return AgentMeta.from_dict(data) if data else None

    async def update_agent_meta(self, meta: AgentMeta) -> AgentMeta:
        meta.updated_at = _iso_now()
        async with self._lock:
            _atomic_write_json(self._agent_meta_path(meta.agent_id), meta.to_dict())
        return meta

    async def write_agent_meta_raw(self, meta: AgentMeta) -> AgentMeta:
        """Write agent meta without refreshing updated_at (for backdating in
        retention tests / explicit timestamp control)."""
        async with self._lock:
            _atomic_write_json(self._agent_meta_path(meta.agent_id), meta.to_dict())
        return meta

    async def set_active_task(
        self,
        agent_id: str,
        task_id: str,
        status: str,
    ) -> AgentMeta | None:
        async with self._lock:
            data = _read_json(self._agent_meta_path(agent_id))
            if data is None:
                return None
            meta = AgentMeta.from_dict(data)
            meta.active_task_id = task_id
            meta.last_task_id = task_id
            meta.last_status = status
            meta.updated_at = _iso_now()
            _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
            return meta

    async def clear_active_task(
        self,
        agent_id: str,
        *,
        last_status: str = "",
    ) -> AgentMeta | None:
        async with self._lock:
            data = _read_json(self._agent_meta_path(agent_id))
            if data is None:
                return None
            meta = AgentMeta.from_dict(data)
            meta.active_task_id = ""
            if last_status:
                meta.last_status = last_status
            meta.updated_at = _iso_now()
            _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
            return meta

    # -------------------------------------------------------------- run meta

    async def create_run(self, run: RunMeta) -> RunMeta:
        run.created_at = _iso_now()
        run.updated_at = run.created_at
        async with self._lock:
            self._runs_dir(run.agent_id).mkdir(parents=True, exist_ok=True)
            _atomic_write_json(self._run_meta_path(run.agent_id, run.task_id), run.to_dict())
        return run

    async def load_run(self, agent_id: str, task_id: str) -> RunMeta | None:
        async with self._lock:
            data = _read_json(self._run_meta_path(agent_id, task_id))
        return RunMeta.from_dict(data) if data else None

    async def update_run(
        self,
        agent_id: str,
        task_id: str,
        *,
        status: str | None = None,
        mode: str | None = None,
        background_reason: str | None = None,
        started_at: str | None = None,
        result: str | None = None,
        error: str | None = None,
        output_file: str | None = None,
        notification: PendingNotification | None = None,
    ) -> RunMeta | None:
        async with self._lock:
            data = _read_json(self._run_meta_path(agent_id, task_id))
            if data is None:
                return None
            run = RunMeta.from_dict(data)
            if status is not None:
                run.status = status
            if mode is not None:
                run.mode = mode
            if background_reason is not None:
                run.background_reason = background_reason
            if started_at is not None:
                run.started_at = started_at
            if result is not None:
                run.result = result
            if error is not None:
                run.error = error
            if output_file is not None:
                run.output_file = output_file
            if notification is not None:
                run.notification = notification
            run.updated_at = _iso_now()
            if status in TERMINAL_RUN_STATUSES and not run.ended_at:
                run.ended_at = run.updated_at
            _atomic_write_json(self._run_meta_path(agent_id, task_id), run.to_dict())
            return run

    async def finalize_run(
        self,
        agent_id: str,
        task_id: str,
        *,
        status: str,
        result: str = "",
        error: str = "",
        output_file: str = "",
    ) -> RunMeta | None:
        """Atomically write terminal status AND the pending notification so the
        result is discoverable even if the process dies right after."""
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"非法终态: {status}")
        async with self._lock:
            data = _read_json(self._run_meta_path(agent_id, task_id))
            if data is None:
                return None
            run = RunMeta.from_dict(data)
            if run.status in TERMINAL_RUN_STATUSES:
                if run.notification is None and run.status != "interrupted":
                    logger.warning(
                        "[大小姐模式] run 已处于终态但缺少 notification，拒绝重复 finalize: "
                        "agent_id=%s task_id=%s status=%s",
                        agent_id,
                        task_id,
                        run.status,
                    )
                return run
            run.status = status
            run.result = result
            run.error = error
            if not output_file and (result or error):
                output_path = self._output_path(agent_id, task_id)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
                tmp_path.write_text(result or error, encoding="utf-8")
                os.replace(tmp_path, output_path)
                output_file = str(output_path)
            run.output_file = output_file or run.output_file
            run.updated_at = _iso_now()
            run.ended_at = run.updated_at
            notification = PendingNotification(
                notification_id=uuid.uuid4().hex,
                agent_id=agent_id,
                task_id=task_id,
                unified_msg_origin=run.unified_msg_origin,
                status=status,
                result=result,
                error=error,
            )
            run.notification = notification
            _atomic_write_json(self._run_meta_path(agent_id, task_id), run.to_dict())
            meta_data = _read_json(self._agent_meta_path(agent_id))
            if meta_data is not None:
                meta = AgentMeta.from_dict(meta_data)
                if meta.active_task_id == task_id:
                    meta.active_task_id = ""
                meta.last_task_id = task_id
                meta.last_status = status
                meta.updated_at = run.updated_at
                _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
            return run

    async def claim_notification(
        self,
        agent_id: str,
        task_id: str,
    ) -> RunMeta | None:
        """Mark the run's pending notification as delivered (claimed) so a
        subsequent wake does not redeliver it."""
        async with self._lock:
            data = _read_json(self._run_meta_path(agent_id, task_id))
            if data is None:
                return None
            run = RunMeta.from_dict(data)
            if run.notification is None:
                return run
            run.notification.delivered = True
            run.notification.delivered_at = _iso_now()
            run.updated_at = _iso_now()
            _atomic_write_json(self._run_meta_path(agent_id, task_id), run.to_dict())
            return run

    async def interrupt_run(self, agent_id: str, task_id: str) -> RunMeta | None:
        """Persist a silent interrupted terminal caused by shutdown/restart."""
        async with self._lock:
            data = _read_json(self._run_meta_path(agent_id, task_id))
            if data is None:
                return None
            run = RunMeta.from_dict(data)
            if run.status not in ACTIVE_RUN_STATUSES:
                return run
            run.status = "interrupted"
            run.updated_at = _iso_now()
            run.ended_at = run.updated_at
            run.error = run.error or "插件停止时任务仍在运行，已标记为 interrupted。"
            run.notification = None
            _atomic_write_json(self._run_meta_path(agent_id, task_id), run.to_dict())
            meta_data = _read_json(self._agent_meta_path(agent_id))
            if meta_data is not None:
                meta = AgentMeta.from_dict(meta_data)
                if meta.active_task_id == task_id:
                    meta.active_task_id = ""
                meta.last_task_id = task_id
                meta.last_status = "interrupted"
                meta.updated_at = run.updated_at
                _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
            return run

    # --------------------------------------------------------------- transcript

    async def append_message(
        self,
        agent_id: str,
        message: dict[str, Any] | Any,
    ) -> None:
        """Append a single conversation message (role/content/tool_calls/...) to
        the agent's append-only transcript."""
        if hasattr(message, "model_dump"):
            try:
                message = message.model_dump()
            except Exception:
                message = {"repr": repr(message)}
        if not isinstance(message, dict):
            message = {"repr": str(message)}
        async with self._lock:
            _append_jsonl(self._transcript_path(agent_id), message)
            self._touch_agent_unlocked(agent_id)

    async def append_control(
        self,
        agent_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "_control": True,
            "kind": kind,
            "ts": _iso_now(),
            **(payload or {}),
        }
        async with self._lock:
            _append_jsonl(self._transcript_path(agent_id), record)
            self._touch_agent_unlocked(agent_id)

    def _touch_agent_unlocked(self, agent_id: str) -> None:
        data = _read_json(self._agent_meta_path(agent_id))
        if data is None:
            return
        meta = AgentMeta.from_dict(data)
        meta.updated_at = _iso_now()
        _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())

    async def load_transcript(self, agent_id: str) -> list[dict[str, Any]]:
        async with self._lock:
            return _read_jsonl(self._transcript_path(agent_id))

    async def load_run_transcript(
        self,
        agent_id: str,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """Return records between one run's start/end control markers.

        The end marker is optional so an active run can be inspected while it
        is still appending records.
        """
        normalized_task_id = _validate_hex_id(task_id, "task_id")
        records = await self.load_transcript(agent_id)
        selected: list[dict[str, Any]] = []
        inside = False
        for record in records:
            if record.get("_control"):
                kind = str(record.get("kind") or "")
                record_task_id = str(record.get("task_id") or "").strip().casefold()
                if kind == CTRL_RUN_START and record_task_id == normalized_task_id:
                    inside = True
                    selected = []
                    continue
                if kind == CTRL_RUN_END and record_task_id == normalized_task_id:
                    if inside:
                        break
                    continue
            if inside:
                selected.append(record)
        return selected

    async def rebuild_contexts_for_resume(
        self,
        agent_id: str,
    ) -> list[dict[str, Any]]:
        """Rebuild openai-format contexts from the transcript for resume.

        Drops the corrupted tail (handled in _read_jsonl) and truncates any
        trailing assistant message whose tool_calls lack matching tool results.
        """
        records = await self.load_transcript(agent_id)
        contexts: list[dict[str, Any]] = []
        pending_tool_call_positions: dict[str, int] = {}
        for record in records:
            if record.get("_control"):
                continue
            role = str(record.get("role") or "")
            if role == "assistant":
                tool_calls = record.get("tool_calls")
                if isinstance(tool_calls, list):
                    for call in tool_calls:
                        call_id = ""
                        if isinstance(call, dict):
                            call_id = str(call.get("id") or "")
                        if call_id:
                            pending_tool_call_positions[call_id] = len(contexts)
                contexts.append(record)
            elif role == "tool":
                call_id = str(record.get("tool_call_id") or "")
                if call_id:
                    pending_tool_call_positions.pop(call_id, None)
                contexts.append(record)
            else:
                contexts.append(record)

        if pending_tool_call_positions:
            contexts = contexts[: min(pending_tool_call_positions.values())]
        return contexts

    # ---------------------------------------------------------------- outputs

    async def write_output(
        self,
        agent_id: str,
        task_id: str,
        content: str,
    ) -> str:
        path = self._output_path(agent_id, task_id)

        def _write() -> str:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
            return str(path)

        return await asyncio.to_thread(_write)

    # ------------------------------------------------------------ enumeration

    async def list_agent_ids(self) -> list[str]:
        async with self._lock:
            if not self.agents_dir.exists():
                return []
            return sorted(
                p.name
                for p in self.agents_dir.iterdir()
                if p.is_dir() and _AGENT_ID_RE.fullmatch(p.name)
            )

    async def list_runs(self, agent_id: str) -> list[RunMeta]:
        runs: list[RunMeta] = []
        async with self._lock:
            runs_dir = self._runs_dir(agent_id)
            if not runs_dir.exists():
                return runs
            for entry in sorted(runs_dir.iterdir()):
                if not entry.is_file() or entry.suffix != ".json":
                    continue
                data = _read_json(entry)
                if data is not None:
                    runs.append(RunMeta.from_dict(data))
        return runs

    async def find_run(self, task_id: str) -> tuple[AgentMeta, RunMeta] | None:
        normalized = _validate_hex_id(task_id, "task_id")
        async with self._lock:
            for agent_id in await self._list_agent_ids_unlocked():
                run_data = _read_json(self._run_meta_path(agent_id, normalized))
                if run_data is None:
                    continue
                agent_data = _read_json(self._agent_meta_path(agent_id))
                if agent_data is None:
                    continue
                return AgentMeta.from_dict(agent_data), RunMeta.from_dict(run_data)
        return None

    async def delete_agent(self, agent_id: str) -> bool:
        """Delete one runtime agent and all transcript/run/output files."""
        async with self._lock:
            agent_dir = self._agent_dir(agent_id)
            if not agent_dir.exists():
                return False
            await self._rm_tree(agent_dir)
            return not agent_dir.exists()

    async def list_pending_notifications(
        self,
        unified_msg_origin: str,
    ) -> list[tuple[RunMeta, PendingNotification]]:
        """Snapshot all undelivered notifications for a UMO (Claude's
        opportunistic snapshot semantics)."""
        snapshot: list[tuple[RunMeta, PendingNotification]] = []
        async with self._lock:
            for agent_id in await self._list_agent_ids_unlocked():
                runs_dir = self._runs_dir(agent_id)
                if not runs_dir.exists():
                    continue
                for entry in runs_dir.iterdir():
                    if not entry.is_file() or entry.suffix != ".json":
                        continue
                    data = _read_json(entry)
                    if data is None:
                        continue
                    run = RunMeta.from_dict(data)
                    if run.unified_msg_origin != unified_msg_origin:
                        continue
                    if run.notification is None or run.notification.delivered:
                        continue
                    snapshot.append((run, run.notification))
        snapshot.sort(key=lambda item: (item[1].created_at, item[1].task_id))
        return snapshot

    async def _list_agent_ids_unlocked(self) -> list[str]:
        if not self.agents_dir.exists():
            return []
        return sorted(
            p.name
            for p in self.agents_dir.iterdir()
            if p.is_dir() and _AGENT_ID_RE.fullmatch(p.name)
        )

    # ------------------------------------------------------------- retention

    async def prune_inactive(self, retention_days: int) -> int:
        """Delete agents (and their transcript/runs/outputs) inactive for longer
        than retention_days. Memory directories and legacy sessions/*.json are
        intentionally untouched."""
        if retention_days <= 0:
            return 0
        cutoff = _utcnow() - timedelta(days=retention_days)
        removed = 0
        async with self._lock:
            if not self.agents_dir.exists():
                return 0
            for agent_id in await self._list_agent_ids_unlocked():
                meta_data = _read_json(self._agent_meta_path(agent_id))
                if meta_data is None:
                    # No metadata: treat as removable only if its dir is old.
                    agent_dir = self._agent_dir(agent_id)
                    mtime = datetime.fromtimestamp(agent_dir.stat().st_mtime, UTC)
                    if mtime < cutoff:
                        await self._rm_tree(agent_dir)
                        removed += 1
                    continue
                meta = AgentMeta.from_dict(meta_data)
                try:
                    updated = datetime.fromisoformat(meta.updated_at)
                except ValueError:
                    updated = _utcnow()
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                if updated >= cutoff:
                    continue
                await self._rm_tree(self._agent_dir(agent_id))
                removed += 1
        if removed:
            logger.info(
                "[大小姐模式] runtime retention 清理 %d 个超期 agent (retention=%dd)",
                removed,
                retention_days,
            )
        return removed

    async def _rm_tree(self, path: Path) -> None:
        def _rm() -> None:
            import shutil

            shutil.rmtree(path, ignore_errors=False)

        try:
            await asyncio.to_thread(_rm)
        except OSError as exc:
            logger.warning(
                "[大小姐模式] 删除 runtime agent 目录失败: path=%s err=%s",
                path,
                exc,
            )

    # --------------------------------------------------- restart reconciliation

    async def reconcile_on_restart(self) -> list[RunMeta]:
        """Collapse any starting/running runs to 'interrupted' on plugin start.

        Returns the reconciled runs so the orchestrator can surface them.
        Silent: no auto-replay, no proactive notification. Per the 1.3.0 spec."""
        reconciled: list[RunMeta] = []
        async with self._lock:
            for agent_id in await self._list_agent_ids_unlocked():
                meta_data = _read_json(self._agent_meta_path(agent_id))
                if meta_data is None:
                    continue
                meta = AgentMeta.from_dict(meta_data)
                runs_dir = self._runs_dir(agent_id)
                if runs_dir.exists():
                    for entry in runs_dir.iterdir():
                        if not entry.is_file() or entry.suffix != ".json":
                            continue
                        run_data = _read_json(entry)
                        if run_data is None:
                            continue
                        run = RunMeta.from_dict(run_data)
                        if run.status not in ACTIVE_RUN_STATUSES:
                            continue
                        run.status = "interrupted"
                        run.updated_at = _iso_now()
                        run.ended_at = run.updated_at
                        run.error = (
                            run.error
                            or "插件重启时仍在运行，已标记为 interrupted。"
                        )
                        run.notification = None
                        _atomic_write_json(entry, run.to_dict())
                        reconciled.append(run)
                if meta.active_task_id:
                    active_data = _read_json(
                        self._run_meta_path(agent_id, meta.active_task_id)
                    )
                    active_run = RunMeta.from_dict(active_data) if active_data else None
                    if active_run is None or active_run.status not in ACTIVE_RUN_STATUSES:
                        meta.active_task_id = ""
                if any(run.agent_id == agent_id for run in reconciled):
                    meta.active_task_id = ""
                    meta.last_status = "interrupted"
                    meta.last_task_id = next(
                        run.task_id for run in reversed(reconciled) if run.agent_id == agent_id
                    )
                meta.updated_at = _iso_now()
                _atomic_write_json(self._agent_meta_path(agent_id), meta.to_dict())
        if reconciled:
            logger.warning(
                "[大小姐模式] 启动时收敛 %d 个遗留 runtime run 为 interrupted: %s",
                len(reconciled),
                ",".join(r.task_id[:8] for r in reconciled),
            )
        return reconciled
