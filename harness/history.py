"""历史分页与 surface 折叠（对应 apiproxy 的 session.history + surface fold）。

分页规则：一页 = 整数条「surface 消息」及其拥有的裸事件（chunk/工具事件），
绝不从消息中间切开。实现上以 turn 块为最小切块（块边界 = turn/start）：
turn/start..turn/end 连同其内全部消息/块事件一体进出页，天然满足
「整数条消息」约束；maxMessages 以 surface 消息数计，从尾往前取整数个 turn
块直到再取一块会超限。

in-flight partial（尾页）：最后一个 turn 未收尾时，最后一条 surface 消息
之后的「非收尾标记」事件（chunk / tool 事件 / step 标记）。

rewind 语义（maid/rewind 标记，ignorable）：标记 {atSeq} 隐藏
[atSeq, markerSeq) 区间内的事件；标记之后的新事件可见（与旧实现
「目标 run 及其后退出上下文、磁盘不删」一致）。
"""

from __future__ import annotations

from .contracts import SURFACE_EVENT_TYPES

_CLOSERS = {"step/end", "turn/end"}


def rewind_markers(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("type") == "maid/rewind"]


def visible_events(events: list[dict]) -> list[dict]:
    """应用 rewind 可见性。"""
    markers = rewind_markers(events)
    if not markers:
        return events
    hidden: list[tuple[int, int]] = []
    for marker in markers:
        at = int(marker.get("data", {}).get("atSeq", 0))
        hidden.append((at, marker["seq"]))
    out = []
    for event in events:
        seq = event["seq"]
        if any(start <= seq < end for start, end in hidden):
            continue
        out.append(event)
    return out


def derive_surface(events: list[dict]) -> list[dict]:
    """可见的 surface 消息（user/message、assistant/message、tool/result）。"""
    return [e for e in visible_events(events) if e.get("type") in SURFACE_EVENT_TYPES]


def _turn_blocks(events: list[dict]) -> list[list[dict]]:
    """按 turn/start 切块。日志头（第一个 turn/start 之前）算第 0 块。"""
    blocks: list[list[dict]] = []
    current: list[dict] = []
    for event in events:
        if event.get("type") == "turn/start" and current:
            blocks.append(current)
            current = []
        current.append(event)
    if current:
        blocks.append(current)
    return blocks


def _block_surface_count(block: list[dict]) -> int:
    return sum(1 for e in block if e.get("type") in SURFACE_EVENT_TYPES)


def history_page(
    events: list[dict],
    *,
    before_seq: int | None = None,
    max_messages: int = 30,
) -> dict:
    """返回 {events, has_more, partial_from}。partial_from 为索引（仅尾页有值）。"""
    events = visible_events(events)

    if before_seq is not None:
        events = [e for e in events if e["seq"] < before_seq]

    blocks = _turn_blocks(events)

    if not blocks or not any(_block_surface_count(b) for b in blocks):
        # 无 surface 消息：空日志或纯 partial
        return {
            "events": events if before_seq is None else [],
            "has_more": False,
            "partial_from": 0 if (events and before_seq is None) else None,
        }

    chosen: list[list[dict]] = []
    count = 0
    has_more = False
    for block in reversed(blocks):
        block_count = _block_surface_count(block)
        if not block_count and not chosen:
            continue  # 尾部无消息的块（运行中）先跳过？不会发生：open turn 已含块内
        if chosen and count + block_count > max_messages:
            has_more = True
            break
        chosen.insert(0, block)
        count += block_count

    page_events = [e for block in chosen for e in block]

    partial_from = None
    if before_seq is None and chosen:
        last_block = chosen[-1]
        # 跳过紧贴最后一条 surface 的收尾标记，其后即 partial
        idx = max(i for i, e in enumerate(last_block) if e.get("type") in SURFACE_EVENT_TYPES)
        k = idx + 1
        while k < len(last_block) and last_block[k].get("type") in _CLOSERS:
            k += 1
        if k < len(last_block):
            partial_from = sum(len(b) for b in chosen[:-1]) + k

    return {"events": page_events, "has_more": has_more, "partial_from": partial_from}


def in_flight_partial(events: list[dict]) -> list[dict]:
    """未定稿事件：最后一条 surface 消息之后、跳过紧随收尾标记的部分。"""
    events = visible_events(events)
    idx = -1
    for i, e in enumerate(events):
        if e.get("type") in SURFACE_EVENT_TYPES:
            idx = i
    if idx < 0:
        return list(events)
    k = idx + 1
    while k < len(events) and events[k].get("type") in _CLOSERS:
        k += 1
    return events[k:]
