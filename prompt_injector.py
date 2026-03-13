"""
大小姐管家模式插件 - 提示注入器

负责为主模型注入大小姐角色说明。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .constants import MAID_SYSTEM_PROMPT_APPEND

if TYPE_CHECKING:
    from astrbot.api.provider import ProviderRequest


def inject_maid_system_prompt(req: ProviderRequest) -> bool:
    """
    注入大小姐模式说明到系统提示。

    Args:
        req: ProviderRequest 对象

    Returns:
        是否成功注入（如果已存在则返回 False）
    """
    if MAID_SYSTEM_PROMPT_APPEND not in req.system_prompt:
        req.system_prompt += MAID_SYSTEM_PROMPT_APPEND
        return True
    return False
