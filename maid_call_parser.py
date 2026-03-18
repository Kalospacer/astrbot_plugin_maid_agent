"""
大小姐管家模式插件 - XML 调度解析器

负责从主模型自然语言输出中解析管家调度标签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .constants import DEFAULT_CALL_MAID_TAG_NAME, DEFAULT_MAID_AGENT_NAME


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


def _parse_match(match: re.Match[str]) -> MaidCall | None:
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

    if action in {"stop", "done"}:
        return MaidCall(
            agent_name=agent_name,
            request_text="",
            raw_block=raw_block,
            action=action,
        )
    if action == "steer" and body:
        return MaidCall(
            agent_name=agent_name,
            request_text=body,
            raw_block=raw_block,
            action=action,
        )
    if not body:
        return None

    return MaidCall(
        agent_name=agent_name,
        request_text=body,
        raw_block=raw_block,
        action=action,
    )


def parse_maid_calls(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
) -> list[MaidCall]:
    if not text:
        return []

    matches: list[re.Match[str]] = []
    for pattern in _get_call_patterns(call_tag_name):
        matches.extend(pattern.finditer(text))
    matches.sort(key=lambda item: item.start())

    result: list[MaidCall] = []
    for match in matches:
        parsed = _parse_match(match)
        if parsed is not None:
            result.append(parsed)
    return result


def parse_maid_call(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
) -> MaidCall | None:
    calls = parse_maid_calls(text, call_tag_name)
    return calls[0] if calls else None
