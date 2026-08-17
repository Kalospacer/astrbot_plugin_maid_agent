"""一次性迁移器：旧 runtime 数据 → 会话事件日志。

旧形状（<data>/agents/<agent_id>/）:
    agent.json          AgentMeta
    transcript.jsonl    控制记录(_control) + OpenAI 消息记录
    runs/<task>.json    RunMeta

映射：
    run_start/run_end           → turn/start + turn/end（reason 按 RunMeta.status）
    派发 user 消息               → user/message（surface append）
    assistant 消息               → assistant/message（text / tool-call 块）
    tool 角色消息                 → tool/result
    steer 控制记录                → user/message
    rewind 控制记录                → maid/rewind {atSeq: 该任务 turn/start 的 seq}
时间：控制记录带 ts；消息记录无 ts，沿用前一事件时间。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .harness import contracts as c
from .harness.store import SessionStore

_STATUS_REASON = {
    "completed": "completed",
    "failed": "error",
    "stopped": "aborted",
    "interrupted": "interrupted",
}


def _iso_to_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _msg_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(str(part.get("text") or part.get("think") or ""))
        return "".join(parts)
    return ""


def _tool_call_tuple(tc: dict) -> tuple[str, str, str]:
    function = tc.get("function") or {}
    arguments = function.get("arguments") if isinstance(function, dict) else None
    if arguments is None:
        arguments = tc.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {}, ensure_ascii=False)
    return str(tc.get("id") or c.new_id()), str(function.get("name") or tc.get("name") or ""), arguments


def migrate_legacy_agents(store: SessionStore, legacy_root: Path) -> dict:
    """迁移旧 agents 目录；返回 {migrated, skipped, errors}。"""
    migrated: list[str] = []
    errors: list[str] = []
    if not legacy_root.is_dir():
        return {"migrated": migrated, "skipped": [], "errors": errors}

    for agent_dir in sorted(legacy_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        agent_id = agent_dir.name
        agent_json = agent_dir / "agent.json"
        transcript_path = agent_dir / "transcript.jsonl"
        if not agent_json.exists() or not transcript_path.exists():
            continue
        if store.exists(agent_id):
            continue
        try:
            _migrate_one(store, agent_dir, agent_id)
            migrated.append(agent_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{agent_id}: {exc}")
    return {"migrated": migrated, "skipped": [], "errors": errors}


def _migrate_one(store: SessionStore, agent_dir: Path, agent_id: str) -> None:
    with open(agent_dir / "agent.json", "r", encoding="utf-8") as fh:
        agent_meta = json.load(fh)

    runs_dir = agent_dir / "runs"
    run_status: dict[str, str] = {}
    run_times: dict[str, tuple[int | None, int | None]] = {}
    if runs_dir.is_dir():
        for run_file in runs_dir.glob("*.json"):
            try:
                with open(run_file, "r", encoding="utf-8") as fh:
                    run_meta = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            task_id = str(run_meta.get("task_id") or run_file.stem)
            run_status[task_id] = str(run_meta.get("status") or "completed")
            run_times[task_id] = (
                _iso_to_ms(run_meta.get("started_at") or run_meta.get("created_at")),
                _iso_to_ms(run_meta.get("ended_at") or run_meta.get("updated_at")),
            )

    records: list[dict] = []
    with open(agent_dir / "transcript.jsonl", "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                break

    created_ms = _iso_to_ms(agent_meta.get("created_at")) or c.now_ms()
    log = store.create_session(
        session_id=agent_id,
        agent_preset=str(agent_meta.get("agent_name") or "") or None,
        meta={
            "umo": str(agent_meta.get("unified_msg_origin") or ""),
            "senderId": str(agent_meta.get("sender_id") or ""),
            "agentName": str(agent_meta.get("agent_name") or ""),
            "chatOwned": True,
            "notify": True,
            "createdAt": created_ms,
            "updatedAt": _iso_to_ms(agent_meta.get("updated_at")) or created_ms,
            "migratedFrom": "v1",
        },
    )
    # create_session 写了 header；手动补 createdAt
    header = log.load_header() or {}
    header["createdAt"] = created_ms
    import os

    tmp = log.header_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(header, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, log.header_path)

    if agent_meta.get("title"):
        log.append("session/title", {"title": str(agent_meta["title"]), "source": {"kind": "auto"}}, time_ms=created_ms)

    turn = 0
    step = 0
    fallback_time = created_ms
    turn_start_seq: dict[str, int] = {}

    for record in records:
        ts = _iso_to_ms(record.get("ts")) if record.get("_control") else None
        if ts is not None:
            fallback_time = ts
        time_ms = ts or fallback_time

        if record.get("_control"):
            kind = record.get("kind")
            if kind == "run_start":
                turn += 1
                step = 0
                event = log.append("turn/start", {"turn": turn}, time_ms=time_ms)
                turn_start_seq[str(record.get("task_id") or "")] = event["seq"]
            elif kind == "run_end":
                task_id = str(record.get("task_id") or "")
                reason_kind = _STATUS_REASON.get(run_status.get(task_id, "completed"), "completed")
                if reason_kind == "error":
                    reason = c.reason_error("（迁移自旧数据的失败任务）")
                elif reason_kind == "aborted":
                    reason = c.reason_aborted("user")
                else:
                    reason = {"kind": reason_kind}
                log.append("turn/end", {"turn": turn, "reason": reason}, time_ms=time_ms)
            elif kind == "steer":
                message_text = str(record.get("message") or "")
                if message_text:
                    log.append(
                        "user/message",
                        c.user_message([c.text_block(message_text)]),
                        source_event_seqs=[],
                        time_ms=time_ms,
                    )
            elif kind == "rewind":
                at = turn_start_seq.get(str(record.get("task_id") or ""), -1)
                if at >= 0:
                    log.append("maid/rewind", {"atSeq": at}, ignorable=True, time_ms=time_ms)
            continue

        role = record.get("role")
        if role == "user":
            task_id = str(record.get("task_id") or "")
            text = _msg_text(record.get("content"))
            if not text:
                continue
            log.append("user/message", c.user_message([c.text_block(text)]), source_event_seqs=[], time_ms=time_ms)
        elif role == "assistant":
            step += 1
            blocks: list[dict] = []
            text = _msg_text(record.get("content"))
            if text:
                blocks.append(c.text_block(text))
            tool_tuples = [
                _tool_call_tuple(tc) for tc in record.get("tool_calls") or [] if isinstance(tc, dict)
            ]
            for call_id, _n, _a in tool_tuples:
                blocks.append(c.tool_call_block(call_id, _n, _a))
            if not blocks:
                continue
            log.append(
                "assistant/message",
                {
                    "turn": max(turn, 1),
                    "step": max(step, 1),
                    "message": c.assistant_message(blocks, "migrated", "migrated"),
                },
                source_event_seqs=[],
                time_ms=time_ms,
            )
            for call_id, name, arguments in tool_tuples:
                log.append(
                    "tool/call",
                    {"turn": max(turn, 1), "step": max(step, 1), "callId": call_id, "name": name, "arguments": arguments},
                    time_ms=time_ms,
                )
        elif role == "tool":
            call_id = str(record.get("tool_call_id") or c.new_id())
            text = _msg_text(record.get("content"))
            log.append(
                "tool/result",
                {"turn": max(turn, 1), "step": max(step, 1), "message": c.tool_result_message(call_id, [c.text_block(text)], False)},
                source_event_seqs=[],
                time_ms=time_ms,
            )
