"""
大小姐管家模式插件 - XML 调度解析器

负责从主模型自然语言输出中解析 `<call_maid>` 标签。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .constants import CALL_MAID_TAG_NAME, DEFAULT_MAID_AGENT_NAME


@dataclass(slots=True)
class MaidCall:
    """解析出的大小姐调度请求。"""

    agent_name: str
    request_text: str
    raw_block: str


def parse_maid_call(text: str, call_tag_name: str = CALL_MAID_TAG_NAME) -> MaidCall | None:
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

    pattern = re.compile(
        rf"<(?P<tag>{re.escape(call_tag_name)})(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</{re.escape(call_tag_name)}>",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None

    raw_block = match.group(0)
    body = match.group("body").strip()
    attrs = match.group("attrs") or ""

    if not body:
        return None

    agent_match = re.search(r'agent\s*=\s*["\'](?P<agent>[^"\']+)["\']', attrs, re.IGNORECASE)
    agent_name = (agent_match.group("agent").strip() if agent_match else DEFAULT_MAID_AGENT_NAME)
    if not agent_name:
        agent_name = DEFAULT_MAID_AGENT_NAME

    return MaidCall(
        agent_name=agent_name,
        request_text=body,
        raw_block=raw_block,
    )
