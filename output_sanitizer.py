"""
大小姐管家模式插件 - 用户可见输出清洗器
"""

from __future__ import annotations

import re

DEFAULT_CALL_MAID_TAG_NAME = "call_maid"
DEFAULT_DONE_TAG_NAME = "maid_session"


def sanitize_user_visible_output(
    text: str,
    call_tag_name: str = DEFAULT_CALL_MAID_TAG_NAME,
    done_tag_name: str = DEFAULT_DONE_TAG_NAME,
) -> str:
    """清洗用户可见文本中的 `<call_maid>` 标签及残留。"""
    if not text:
        return ""

    sanitized = re.sub(
        rf"<{re.escape(call_tag_name)}\b[^>]*>[\s\S]*?</{re.escape(call_tag_name)}>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        rf"</?{re.escape(call_tag_name)}\b[^>]*>", "", sanitized, flags=re.IGNORECASE
    )
    sanitized = re.sub(
        rf"<{re.escape(done_tag_name)}\b[^>]*/>",
        "",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        rf"<{re.escape(done_tag_name)}\b[^>]*>[\s\S]*?</{re.escape(done_tag_name)}>",
        "",
        sanitized,
        flags=re.IGNORECASE,
    )
    sanitized = re.sub(
        rf"</?{re.escape(done_tag_name)}\b[^>]*>", "", sanitized, flags=re.IGNORECASE
    )
    sanitized = re.sub(r"\n{3,}", "\n\n", sanitized)
    return sanitized.strip()
