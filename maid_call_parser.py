"""
大小姐管家模式插件 - XML 调度解析器

负责从主模型自然语言输出中解析管家调度标签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_MAID_AGENT_NAME = "butler"


@lru_cache(maxsize=32)
def _get_call_patterns(call_tag_name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    return (
        re.compile(
            rf"<(?P<tag>{re.escape(call_tag_name)})(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</{re.escape(call_tag_name)}>",
            re.IGNORECASE,
        ),
        re.compile(
            rf"<(?P<tag>{re.escape(call_tag_name)})(?P<attrs>[^>]*)/>",
            re.IGNORECASE,
        ),
    )


@dataclass(slots=True)
class MaidCall:
    agent_name: str
    request_text: str
    raw_block: str
    action: str = ""
    turns: int = 0


def parse_maid_call(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
) -> MaidCall | None:
    if not text:
        return None

    match = None
    for pattern in _get_call_patterns(call_tag_name):
        match = pattern.search(text)
        if match:
            break
    if not match:
        return None

    raw_block = match.group(0)
    body = (match.groupdict().get("body") or "").strip()
    attrs = match.group("attrs") or ""

    agent_match = re.search(r'agent\s*=\s*["\'](?P<agent>[^"\']+)["\']', attrs, re.IGNORECASE)
    agent_name = agent_match.group("agent").strip() if agent_match else DEFAULT_MAID_AGENT_NAME
    if not agent_name:
        agent_name = DEFAULT_MAID_AGENT_NAME

    action_match = re.search(
        r'action\s*=\s*["\'](?P<action>[^"\']+)["\']',
        attrs,
        re.IGNORECASE,
    )
    action = action_match.group("action").strip().casefold() if action_match else ""

    turns_match = re.search(
        r'turns\s*=\s*["\'](?P<turns>\d+)["\']',
        attrs,
        re.IGNORECASE,
    )
    turns = int(turns_match.group("turns")) if turns_match else 0

    if action in {"status", "stop", "done"}:
        return MaidCall(
            agent_name=agent_name,
            request_text="",
            raw_block=raw_block,
            action=action,
            turns=turns,
        )
    if action == "continue" and turns > 0:
        return MaidCall(
            agent_name=agent_name,
            request_text="",
            raw_block=raw_block,
            action=action,
            turns=turns,
        )
    if action == "steer" and body:
        return MaidCall(
            agent_name=agent_name,
            request_text=body,
            raw_block=raw_block,
            action=action,
            turns=turns,
        )
    if not body:
        return None

    return MaidCall(
        agent_name=agent_name,
        request_text=body,
        raw_block=raw_block,
        action=action,
        turns=turns,
    )
