"""
大小姐管家模式插件 - XML 调度解析器

负责从主模型自然语言输出中解析 `<call_maid>` 标签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_DONE_TAG_NAME = "maid_session"
DEFAULT_CONTROL_TAG_NAME = "maid_control"
DEFAULT_MAID_AGENT_NAME = "butler"


@lru_cache(maxsize=32)
def _get_done_tag_patterns(done_tag_name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    return (
        re.compile(
            rf"<{re.escape(done_tag_name)}(?P<attrs>[^>]*)/>",
            re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(done_tag_name)}(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</{re.escape(done_tag_name)}>",
            re.IGNORECASE,
        ),
    )


@lru_cache(maxsize=32)
def _get_call_pattern(call_tag_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"<(?P<tag>{re.escape(call_tag_name)})(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</{re.escape(call_tag_name)}>",
        re.IGNORECASE,
    )


@lru_cache(maxsize=32)
def _get_control_patterns(control_tag_name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    return (
        re.compile(
            rf"<{re.escape(control_tag_name)}(?P<attrs>[^>]*)/>",
            re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(control_tag_name)}(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</{re.escape(control_tag_name)}>",
            re.IGNORECASE,
        ),
    )


@dataclass(slots=True)
class MaidCall:
    """解析出的大小姐调度请求。"""

    agent_name: str
    request_text: str
    raw_block: str


@dataclass(slots=True)
class MaidControl:
    action: str
    raw_block: str
    request_text: str = ""


def parse_maid_session_done(
    text: str,
    done_tag_name: str = DEFAULT_DONE_TAG_NAME,
) -> bool:
    """判断文本中是否包含 session 结束标签。"""
    if not text:
        return False

    for pattern in _get_done_tag_patterns(done_tag_name):
        for match in pattern.finditer(text):
            attrs = match.groupdict().get("attrs") or ""
            status_match = re.search(
                r'status\s*=\s*["\'](?P<status>[^"\']+)["\']',
                attrs,
                re.IGNORECASE,
            )
            if status_match and status_match.group("status").strip().casefold() == "done":
                return True

    return False


def parse_maid_call(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
) -> MaidCall | None:
    """
    从文本中解析第一个 `<call_maid>` 块。

    Args:
        text: 模型输出文本
        call_tag_name: XML 标签名

    Returns:
        解析出的 MaidCall；若未找到合法标签则返回 None
    """
    if not text:
        return None

    pattern = _get_call_pattern(call_tag_name)
    match = pattern.search(text)
    if not match:
        return None

    raw_block = match.group(0)
    body = match.group("body").strip()
    attrs = match.group("attrs") or ""

    if not body:
        return None

    agent_match = re.search(r'agent\s*=\s*["\'](?P<agent>[^"\']+)["\']', attrs, re.IGNORECASE)
    agent_name = agent_match.group("agent").strip() if agent_match else DEFAULT_MAID_AGENT_NAME
    if not agent_name:
        agent_name = DEFAULT_MAID_AGENT_NAME

    return MaidCall(
        agent_name=agent_name,
        request_text=body,
        raw_block=raw_block,
    )


def parse_maid_control(
    text: str,
    control_tag_name: str = DEFAULT_CONTROL_TAG_NAME,
) -> MaidControl | None:
    if not text:
        return None

    for pattern in _get_control_patterns(control_tag_name):
        match = pattern.search(text)
        if not match:
            continue
        raw_block = match.group(0)
        attrs = match.groupdict().get("attrs") or ""
        body = (match.groupdict().get("body") or "").strip()
        action_match = re.search(
            r'action\s*=\s*["\'](?P<action>[^"\']+)["\']',
            attrs,
            re.IGNORECASE,
        )
        action = action_match.group("action").strip().casefold() if action_match else ""
        if action in {"status", "stop"}:
            return MaidControl(action=action, raw_block=raw_block)
        if action == "steer" and body:
            return MaidControl(action=action, raw_block=raw_block, request_text=body)
    return None
