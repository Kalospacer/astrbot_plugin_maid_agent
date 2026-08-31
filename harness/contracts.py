"""契约词表：事件、消息、内容块、流块、帧的纯构造器。

字段名与前端 TS 侧逐一对齐（zod 校验的闭合词表，
Python 侧用构造器保证写入形状，读取侧按 type 判别）。
"""

from __future__ import annotations

import time
import uuid

SESSION_FORMAT_VERSION = 0


def new_id() -> str:
    return uuid.uuid4().hex


def now_ms() -> int:
    return int(time.time() * 1000)


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def reasoning_block(text: str) -> dict:
    return {"type": "reasoning", "text": text}


def image_block(attachment: dict) -> dict:
    return {"type": "image", "attachment": attachment}


def tool_call_block(call_id: str, name: str, arguments: str) -> dict:
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def tool_result_block(tool_call_id: str, content: list[dict], is_error: bool = False) -> dict:
    return {
        "type": "tool-result",
        "toolCallId": tool_call_id,
        "content": content,
        "isError": is_error,
    }


def user_message(content: list[dict], source: dict | None = None) -> dict:
    return {
        "id": new_id(),
        "role": "user",
        "content": content,
        "source": source or {"kind": "user"},
    }


def user_rpc_message(content: list[dict], rpc_id: str, client_time_zone: str | None = None) -> dict:
    source: dict = {"kind": "user", "rpcId": rpc_id}
    if client_time_zone:
        source["clientTimeZone"] = client_time_zone
    return user_message(content, source)


def assistant_message(content: list[dict], provider: str, model: str) -> dict:
    return {
        "id": new_id(),
        "role": "assistant",
        "content": content,
        "source": {"kind": "model", "provider": provider, "model": model},
    }


def tool_result_message(call_id: str, content: list[dict], is_error: bool) -> dict:
    return {
        "id": new_id(),
        "role": "user",
        "content": [tool_result_block(call_id, content, is_error)],
        "source": {"kind": "tool", "callId": call_id},
    }


def block_start_chunk(index: int, block_type: str) -> dict:
    return {"type": "block-start", "index": index, "blockType": block_type}


def text_delta_chunk(index: int, text: str) -> dict:
    return {"type": "text-delta", "index": index, "text": text}


def reasoning_delta_chunk(index: int, text: str) -> dict:
    return {"type": "reasoning-delta", "index": index, "text": text}


def tool_call_delta_chunk(index: int, call_id: str, name: str | None, arguments_delta: str) -> dict:
    chunk: dict = {"type": "tool-call-delta", "index": index, "id": call_id, "argumentsDelta": arguments_delta}
    if name is not None:
        chunk["name"] = name
    return chunk


def block_end_chunk(index: int, block: dict) -> dict:
    return {"type": "block-end", "index": index, "block": block}


def usage_chunk(usage: dict) -> dict:
    return {"type": "usage", "usage": usage}


def finish_chunk(reason: dict) -> dict:
    return {"type": "finish", "reason": reason}


SURFACE_EVENT_TYPES = {"user/message", "assistant/message", "tool/result"}

KNOWN_EVENT_TYPES = SURFACE_EVENT_TYPES | {
    "turn/start",
    "turn/end",
    "step/start",
    "step/end",
    "assistant/chunk",
    "tool/call",
    "todo/write",
    "request/header",
    "request/context",
    "session/end-seed",
    "session/title",
    "maid/rewind",
    "maid/notification",
}


def make_event(
    event_type: str,
    seq: int,
    data: dict,
    *,
    ignorable: bool = False,
    surface_op: str | None = "append",
    source_event_seqs: list[int] | None = None,
    time_ms: int | None = None,
) -> dict:
    """构造一条事件。surface 字段只允许出现在 surface 事件上。"""
    event: dict = {
        "type": event_type,
        "seq": seq,
        "time": time_ms if time_ms is not None else now_ms(),
        "data": data,
    }
    if ignorable:
        event["ignorable"] = True
    if event_type in SURFACE_EVENT_TYPES:
        if surface_op is not None:
            event["surfaceOp"] = surface_op
        if source_event_seqs is not None:
            event["sourceEventSeqs"] = source_event_seqs
    return event


def reason_completed() -> dict:
    return {"kind": "completed"}


def reason_aborted(kind: str = "user") -> dict:
    return {"kind": "aborted", "reason": {"kind": kind}}


def reason_error(message: str, code: str = "UNKNOWN") -> dict:
    return {"kind": "error", "error": {"message": message, "code": code}}


def reason_max_tokens() -> dict:
    return {"kind": "max-tokens"}


def reason_interrupted() -> dict:
    return {"kind": "interrupted"}


def reason_blocked() -> dict:
    return {"kind": "blocked"}


def generic_call_view(title: str, kind: str = "other", raw_input=None, locations=None) -> dict:
    view: dict = {"card": "generic", "title": title, "kind": kind}
    if raw_input is not None:
        view["rawInput"] = raw_input
    if locations is not None:
        view["locations"] = locations
    return view


def terminal_call_view(title: str, description: str | None = None, cwd: str | None = None) -> dict:
    view: dict = {"card": "terminal", "title": title}
    if description is not None:
        view["description"] = description
    if cwd is not None:
        view["cwd"] = cwd
    return view


def diff_call_view(title: str, diffs: list[dict], locations=None) -> dict:
    view: dict = {"card": "diff", "title": title, "diffs": diffs}
    if locations is not None:
        view["locations"] = locations
    return view


def generic_result_view(title: str | None = None, content: list[dict] | None = None) -> dict:
    view: dict = {"card": "generic"}
    if title is not None:
        view["title"] = title
    if content is not None:
        view["content"] = content
    return view


def terminal_result_view(output: str | None = None, title: str | None = None) -> dict:
    view: dict = {"card": "terminal"}
    if title is not None:
        view["title"] = title
    if output is not None:
        view["output"] = output
    return view


def read_result_view(
    path: str,
    offset: int,
    lines: list[dict],
    total_lines: int,
    title: str | None = None,
    lang: str | None = None,
) -> dict:
    view: dict = {
        "card": "read",
        "path": path,
        "offset": offset,
        "lines": lines,
        "totalLines": total_lines,
    }
    if title is not None:
        view["title"] = title
    if lang is not None:
        view["lang"] = lang
    return view


def tool_event_view_call(view: dict) -> dict:
    return {"for": "call", "view": view}


def tool_event_view_result(view: dict) -> dict:
    return {"for": "result", "view": view}


def frame_session_event(session_id: str, event: dict, view: dict | None = None) -> dict:
    payload: dict = {"type": "session/event", "sessionId": session_id, "event": event}
    if view is not None:
        payload["view"] = view
    return payload


def frame_session_subscribed(session_id: str, last_seq: int) -> dict:
    return {"type": "session/subscribed", "sessionId": session_id, "lastSeq": last_seq}


def frame_session_queue(session_id: str, items: list[dict]) -> dict:
    return {"type": "session/queue", "sessionId": session_id, "items": items}


def frame_session_projection(session_id: str, key: str, value, seq: int) -> dict:
    return {"type": "session/projection", "sessionId": session_id, "key": key, "value": value, "seq": seq}


def frame_stream_error(message: str, code: str = "internal") -> dict:
    return {"type": "stream/error", "error": {"code": code, "message": message, "details": {}}}


def frame_host_session_added(session_id: str, blank: bool, **extra) -> dict:
    payload: dict = {"type": "host/session-added", "sessionId": session_id, "blank": blank}
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def frame_host_session_removed(session_id: str) -> dict:
    return {"type": "host/session-removed", "sessionId": session_id}


def frame_host_session_status(session_id: str, running: bool) -> dict:
    return {"type": "host/session-status", "sessionId": session_id, "running": running}


def frame_host_agent_error(session_id: str, message: str) -> dict:
    return {"type": "host/agent-error", "sessionId": session_id, "message": message}


def is_token_delta(chunk: dict) -> bool:
    """chunk 是否携带可见模型输出（首 token 边界判定，供统计用）。"""
    ctype = chunk.get("type")
    if ctype in ("text-delta", "reasoning-delta"):
        return chunk.get("text") != ""
    if ctype == "tool-call-delta":
        return chunk.get("argumentsDelta") != "" or chunk.get("name") is not None
    return False
