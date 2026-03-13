"""
大小姐管家模式插件 - 响应验证器

负责验证和记录 LLM 响应，确保大小姐模式的输出符合预期。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from astrbot.api import logger

from .constants import BUTLER_HANDOFF_TOOL_NAME

if TYPE_CHECKING:
    from astrbot.api.provider import LLMResponse


def validate_llm_response(resp: LLMResponse) -> list[str]:
    """
    验证 LLM 响应，检查是否正确调用了管家 handoff。

    Args:
        resp: LLMResponse 对象

    Returns:
        尝试直接调用的非管家工具名称列表（空列表表示正常）
    """
    if not resp.tools_call_name:
        return []

    return [
        name
        for name in resp.tools_call_name
        if name != BUTLER_HANDOFF_TOOL_NAME
    ]
