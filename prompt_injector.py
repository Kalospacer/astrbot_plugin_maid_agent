"""
大小姐管家模式插件 - 协议提示注入器

负责为主模型注入 XML 调度协议说明。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from astrbot.api.provider import ProviderRequest

logger = logging.getLogger(__name__)


def build_maid_system_prompt_append(
    call_tag_name: str,
    default_agent_name: str,
    prompt_template: str,
) -> str:
    cleaned_template = prompt_template
    if "{serving_max_turns}" in cleaned_template:
        logger.warning(
            "Detected deprecated placeholder '{serving_max_turns}' in maid system "
            "prompt template; it will be stripped. Please remove it from "
            "'main_system_prompt_template'."
        )
        cleaned_template = cleaned_template.replace("{serving_max_turns}", "")
    return cleaned_template.replace("{call_tag_name}", call_tag_name).replace(
        "{default_agent_name}", default_agent_name
    )


def inject_maid_system_prompt(
    req: ProviderRequest,
    call_tag_name: str,
    default_agent_name: str,
    prompt_template: str,
) -> bool:
    """
    注入大小姐模式的 XML 协议说明到系统提示。

    Args:
        req: ProviderRequest 对象

    Returns:
        是否成功注入（如果已存在则返回 False）
    """
    prompt_append = build_maid_system_prompt_append(
        call_tag_name,
        default_agent_name,
        prompt_template,
    )
    current_prompt = req.system_prompt or ""
    normalized_append = (
        prompt_append
        if not current_prompt or current_prompt.endswith("\n")
        else f"\n{prompt_append}"
    )
    if prompt_append not in current_prompt:
        req.system_prompt = current_prompt + normalized_append
        return True
    return False
