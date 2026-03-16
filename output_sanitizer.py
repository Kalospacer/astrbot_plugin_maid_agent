"""
大小姐管家模式插件 - 对外可见输出清洗器
"""

from __future__ import annotations

import re
from functools import lru_cache

from .constants import DEFAULT_CALL_MAID_TAG_NAME


@lru_cache(maxsize=32)
def _get_sanitize_patterns(call_tag_name: str) -> tuple[re.Pattern[str], ...]:
    return (
        re.compile(
            rf"<{re.escape(call_tag_name)}\b[^>]*>[\s\S]*?</{re.escape(call_tag_name)}>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(call_tag_name)}\b[^>]*/>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"</?{re.escape(call_tag_name)}\b[^>]*>",
            flags=re.IGNORECASE,
        ),
    )


def sanitize_user_visible_output(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
) -> str:
    """清洗对外可见文本中的 `<call_maid>` 标签及残留。"""
    if not text:
        return ""

    sanitized = text
    for pattern in _get_sanitize_patterns(call_tag_name):
        sanitized = pattern.sub("", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()
