"""
大小姐管家模式插件 - 用户可见输出清洗器
"""

from __future__ import annotations

import re
from functools import lru_cache

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_DONE_TAG_NAME = "maid_session"
DEFAULT_CONTROL_TAG_NAME = "maid_control"


@lru_cache(maxsize=32)
def _get_sanitize_patterns(
    call_tag_name: str,
    done_tag_name: str,
) -> tuple[re.Pattern[str], ...]:
    return (
        re.compile(
            rf"<{re.escape(call_tag_name)}\b[^>]*>[\s\S]*?</{re.escape(call_tag_name)}>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"</?{re.escape(call_tag_name)}\b[^>]*>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(done_tag_name)}\b[^>]*/>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(done_tag_name)}\b[^>]*>[\s\S]*?</{re.escape(done_tag_name)}>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"</?{re.escape(done_tag_name)}\b[^>]*>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(DEFAULT_CONTROL_TAG_NAME)}\b[^>]*/>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"<{re.escape(DEFAULT_CONTROL_TAG_NAME)}\b[^>]*>[\s\S]*?</{re.escape(DEFAULT_CONTROL_TAG_NAME)}>",
            flags=re.IGNORECASE,
        ),
        re.compile(
            rf"</?{re.escape(DEFAULT_CONTROL_TAG_NAME)}\b[^>]*>",
            flags=re.IGNORECASE,
        ),
    )


def sanitize_user_visible_output(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
    done_tag_name: str = DEFAULT_DONE_TAG_NAME,
) -> str:
    """清洗用户可见文本中的 `<call_maid>` / `<maid_session>` 标签及残留。"""
    if not text:
        return ""

    sanitized = text
    for pattern in _get_sanitize_patterns(call_tag_name, done_tag_name):
        sanitized = pattern.sub("", sanitized)
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()
